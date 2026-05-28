"""Scraper générique pour annuaires de producteurs.

Stratégie :
1. Si un sélecteur CSS spécifique est fourni dans le YAML → utilise-le (mode "config")
2. Sinon, tente une cascade d'heuristiques pour identifier les "cartes producteurs"
   (mode "auto") — fonctionne dans 60-70% des cas.

Chaque résultat normalisé :
    {nom, commune, code_postal, latitude, longitude, label}
"""
from __future__ import annotations
import re
import time
import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "RadarFournisseursLocaux/1.0 (associé Super U)"}

# Pour les communes composées : on accepte un préfixe optionnel + le dernier mot composé.
# Exemples capturés correctement :
#   "Saint-Hilaire-Saint-Mesmin (45160)" → "Saint-Hilaire-Saint-Mesmin"
#   "La Souterraine (23300)" → "La Souterraine"
#   "Plaimpied-Givaudins (18340)" → "Plaimpied-Givaudins"
#   "Écluses SAINT-HILAIRE-SAINT-MESMIN (45160)" → "SAINT-HILAIRE-SAINT-MESMIN"
# Pattern : optionnel "La/Le/Les/Aux/Lès/etc." suivi d'UN mot composé (lettres + tirets + apostrophes)
RE_COMMUNE_AVANT_CP = re.compile(
    r"((?:(?:La|Le|Les|Aux|En|Lès|L['’]|D['’]|Sainte?)\s+)?"
    r"[A-ZÀ-Ÿ][A-Za-zÀ-ÿ\-'']{1,60})"
    r"\s*\(\s*(\d{5})\s*\)"
)
# Pattern "12345 VILLE" inversé
RE_CP_AVANT_COMMUNE = re.compile(
    r"(\d{5})\s+([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\-\s']{1,40}?)(?=\s{2,}|$|[,;])"
)
# Bruit à virer à la fin des noms scrapés
RE_BRUIT_FIN_NOM = re.compile(
    r"\s*(?:En savoir \+?|Découvrir|Voir la fiche|Lire la suite|>>|→).*$",
    re.IGNORECASE,
)
# Préfixe "Du 1 Janvier au 31 Décembre" (plages d'ouverture, ex. Visit Limousin)
RE_DATE_PREFIX = re.compile(
    r"^\s*Du\s+\d{1,2}\s+\w+\s+au\s+\d{1,2}\s+\w+\s+", re.IGNORECASE,
)
# Catégories en MAJUSCULES qui suivent le nom (ex. Visit Limousin) → on coupe le nom avant
_CATS_MAJ = [
    "BIÈRES", "BIERES", "FRUITS / LÉGUMES", "FRUITS / LEGUMES", "FRUITS", "LÉGUMES", "LEGUMES",
    "CONFISERIE / CHOCOLAT", "CONFISERIE", "CHOCOLAT", "PLANTES AROMATIQUES", "PLANTES",
    "AROMATIQUES", "VIANDES", "VIANDE", "FROMAGES", "FROMAGE", "PRODUITS LAITIERS",
    "MIEL", "PRODUIT APICOLE", "PRODUITS APICOLES", "PRODUITS DE LA RUCHE",
    "ÉPICERIE", "EPICERIE", "ÉPICE", "EPICE", "ÉPICES", "EPICES",
    "SAFRAN", "HUILES", "HUILE", "VINS", "ESCARGOTS", "CHAMPIGNONS", "PRODUITS LOCAUX",
    "FOIE GRAS", "PRODUITS DE LA FERME", "BOISSONS", "JUS", "PAIN", "BOULANGERIE",
    "PÂTISSERIE", "PATISSERIE", "OEUFS", "ŒUFS", "VOLAILLES", "VOLAILLE",
]
RE_CAT_MAJ = re.compile(r"\s+(?:" + "|".join(re.escape(c) for c in _CATS_MAJ) + r")\b")


def extraire_commune_cp(texte: str) -> tuple[str, str]:
    """Cherche un couple (commune, code_postal) dans un texte libre.
    Privilégie la DERNIÈRE occurrence (souvent la commune réelle, le texte commence par le nom)."""
    if not texte:
        return "", ""
    # Préférence : pattern "VILLE (12345)"
    matches = list(RE_COMMUNE_AVANT_CP.finditer(texte))
    if matches:
        last = matches[-1]
        commune = last.group(1).strip(" ,-")
        return commune, last.group(2)
    # Fallback : "12345 VILLE"
    m = RE_CP_AVANT_COMMUNE.search(texte)
    if m:
        return m.group(2).strip(), m.group(1)
    return "", ""


def nettoyer_nom(nom: str) -> str:
    """Nettoie un nom scrapé :
    - vire le préfixe de dates d'ouverture ("Du 1 Janvier au 31 Décembre")
    - coupe à la catégorie en MAJUSCULES qui suit le nom (BIÈRES, FRUITS / LÉGUMES...)
    - vire le bruit de fin (En savoir +, etc.)
    """
    if not nom:
        return ""
    nom = RE_DATE_PREFIX.sub("", nom)
    nom = RE_BRUIT_FIN_NOM.sub("", nom)
    # Coupe à la première catégorie MAJUSCULES (le nom vient avant)
    m = RE_CAT_MAJ.search(nom)
    if m and m.start() > 3:
        nom = nom[:m.start()]
    return nom.strip(" ,-·")[:120]


def fetch_html(url: str, timeout: int = 10) -> str | None:
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except requests.RequestException:
        return None
    return None


def _resolve_url(href: str, base_url: str) -> str:
    """Résout une URL relative en absolue par rapport à base_url."""
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("//"):
        return "https:" + href
    # URL relative : extraire le scheme + host de base_url
    if base_url and "://" in base_url:
        scheme_host = base_url.split("/", 3)
        base = "/".join(scheme_host[:3])
        if href.startswith("/"):
            return base + href
        return base + "/" + href
    return href


def scrape_avec_config(html: str, config: dict, label_nom: str, base_url: str = "") -> list[dict]:
    """Scrape selon une config explicite. config attendu :
        {
          "selecteur_lien": "a.producteur-card",
          "selecteur_nom": "h3",         # optionnel : sinon texte du lien
          "selecteur_commune": ".ville", # optionnel
          "regex_commune": true,         # extraire commune/CP du texte via regex (défaut true)
        }
    base_url sert à résoudre les liens relatifs en absolus.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    sel_lien = config.get("selecteur_lien") or "a"
    sel_nom = config.get("selecteur_nom")
    sel_commune = config.get("selecteur_commune")
    utiliser_regex = config.get("regex_commune", True)

    for el in soup.select(sel_lien):
        # URL de la fiche détaillée (très précieux pour Benjamin pour cliquer)
        url_fiche = ""
        if el.name == "a" and el.get("href"):
            url_fiche = _resolve_url(el.get("href"), base_url)
        else:
            link_inside = el.select_one("a[href]")
            if link_inside:
                url_fiche = _resolve_url(link_inside.get("href", ""), base_url)
        txt_complet = el.get_text(" ", strip=True)
        # Nom
        if sel_nom:
            nom_el = el.select_one(sel_nom)
            nom = nom_el.get_text(strip=True) if nom_el else ""
        else:
            nom = txt_complet[:100].strip()
        # Commune
        commune, cp = "", ""
        if sel_commune:
            c_el = el.select_one(sel_commune)
            if c_el:
                commune, cp = extraire_commune_cp(c_el.get_text(" ", strip=True))
                if not commune:
                    commune = c_el.get_text(strip=True)
        if utiliser_regex and not commune:
            commune, cp = extraire_commune_cp(txt_complet)
            if commune and sel_nom is None:
                # Nom = ce qui précède la dernière occurrence de la commune dans le texte
                idx = txt_complet.rfind(commune)
                if idx > 0:
                    nom = txt_complet[:idx].strip(" ,(-")
        nom = nettoyer_nom(re.sub(r"\s+", " ", nom))
        if nom and len(nom) > 2:
            out.append({
                "nom": nom,
                "commune": commune,
                "code_postal": cp,
                "latitude": None,
                "longitude": None,
                "label": label_nom,
                "url_fiche": url_fiche,
            })
    return out


def scrape_auto(html: str, label_nom: str, base_url: str = "") -> list[dict]:
    """Heuristiques automatiques quand on n'a pas de config."""
    soup = BeautifulSoup(html, "lxml")
    out = []

    # Heuristique 1 : cartes (article / div.card / div.producer / li.producteur...)
    candidates = soup.select(
        "article, div.card, div.producteur, div.producer, "
        "li.producteur, li.producer, .fiche-producteur, .item-producteur"
    )
    # Heuristique 2 : liens contenant "producteur" / "adherents" / "ferme" dans href
    if not candidates:
        candidates = soup.select(
            "a[href*='producteur'], a[href*='adherent'], "
            "a[href*='ferme'], a[href*='exploitation']"
        )

    for el in candidates:
        nom_el = el.select_one("h2, h3, h4, .titre, .nom, strong") or el
        nom = nom_el.get_text(strip=True)
        if not nom or len(nom) < 3:
            continue
        txt = el.get_text(" ", strip=True)
        commune, cp = extraire_commune_cp(txt)
        nom = nettoyer_nom(re.sub(r"\s+", " ", nom))
        if not nom or len(nom) < 3:
            continue
        # URL de la fiche
        url_fiche = ""
        if el.name == "a" and el.get("href"):
            url_fiche = _resolve_url(el.get("href"), base_url)
        else:
            link = el.select_one("a[href]")
            if link:
                url_fiche = _resolve_url(link.get("href", ""), base_url)
        out.append({
            "nom": nom,
            "commune": commune,
            "code_postal": cp,
            "latitude": None,
            "longitude": None,
            "label": label_nom,
            "url_fiche": url_fiche,
        })

    # Déduplication par (nom, commune)
    vus = set()
    dedup = []
    for r in out:
        key = (r["nom"].lower(), r["commune"].lower())
        if key not in vus:
            vus.add(key)
            dedup.append(r)
    return dedup


def scrape_source(source_def: dict) -> list[dict]:
    """Point d'entrée : reçoit une définition de source et renvoie les producteurs.
    Gère automatiquement la pagination si l'URL contient ?listpage=, ?page= ou ?p=.
    """
    nom = source_def.get("nom", "inconnu")
    url = source_def.get("url")
    if not url:
        return []
    config = source_def.get("config") or {}

    def _scrape_une_page(u: str) -> list[dict]:
        html = fetch_html(u)
        if not html:
            return []
        if config:
            r = scrape_avec_config(html, config, nom, base_url=u)
            if r:
                return r
        return scrape_auto(html, nom, base_url=u)

    # Détection pagination
    pagination_param = None
    for p in ("listpage", "page", "p"):
        if f"{p}=" in url:
            pagination_param = p
            break

    if not pagination_param:
        return _scrape_une_page(url)

    # Pagination : itère jusqu'à ne plus avoir de nouveaux items (max 20 pages de sécurité)
    all_results = []
    vus_noms = set()
    for page_num in range(1, 21):
        page_url = re.sub(rf"({pagination_param}=)\d+", rf"\g<1>{page_num}", url)
        items = _scrape_une_page(page_url)
        if not items:
            break
        nouveaux = 0
        for it in items:
            key = (it.get("nom", "").lower(), it.get("commune", "").lower())
            if key not in vus_noms:
                vus_noms.add(key)
                all_results.append(it)
                nouveaux += 1
        if nouveaux == 0:
            break  # plus de nouveaux items = fin pagination
        time.sleep(0.5)  # politesse
    return all_results
