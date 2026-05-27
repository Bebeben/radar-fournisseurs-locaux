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

# Regex commune : "VILLE (12345)" ou "12345 VILLE"
RE_COMMUNE_CP = re.compile(
    r"(?:(\d{5})\s+([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\-\s']{1,60}))|"
    r"(?:([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\-\s']{1,60})\s*[\(\-,]\s*(\d{5}))"
)


def extraire_commune_cp(texte: str) -> tuple[str, str]:
    """Cherche un couple (CP, commune) dans un texte libre."""
    if not texte:
        return "", ""
    m = RE_COMMUNE_CP.search(texte)
    if not m:
        return "", ""
    if m.group(1):  # forme "12345 VILLE"
        return m.group(2).strip(), m.group(1)
    return m.group(3).strip(), m.group(4)


def fetch_html(url: str, timeout: int = 10) -> str | None:
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except requests.RequestException:
        return None
    return None


def scrape_avec_config(html: str, config: dict, label_nom: str) -> list[dict]:
    """Scrape selon une config explicite. config attendu :
        {
          "selecteur_lien": "a.producteur-card",
          "selecteur_nom": "h3",         # optionnel : sinon texte du lien
          "selecteur_commune": ".ville", # optionnel
          "regex_commune": true,         # extraire commune/CP du texte via regex (défaut true)
        }
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    sel_lien = config.get("selecteur_lien") or "a"
    sel_nom = config.get("selecteur_nom")
    sel_commune = config.get("selecteur_commune")
    utiliser_regex = config.get("regex_commune", True)

    for el in soup.select(sel_lien):
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
            # Nettoyer le nom de la trace ville
            commune, cp = extraire_commune_cp(txt_complet)
            # Si on a trouvé une commune dans le texte global, ajuster le nom
            if commune and sel_nom is None:
                # Le nom devient le début du texte avant la commune
                idx = txt_complet.find(commune)
                if idx > 0:
                    nom = txt_complet[:idx].strip(" ,(-")
        nom = re.sub(r"\s+", " ", nom).strip()
        if nom and len(nom) > 2 and len(nom) < 150:
            out.append({
                "nom": nom,
                "commune": commune,
                "code_postal": cp,
                "latitude": None,
                "longitude": None,
                "label": label_nom,
            })
    return out


def scrape_auto(html: str, label_nom: str) -> list[dict]:
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
        nom = re.sub(r"\s+", " ", nom)[:120].strip()
        out.append({
            "nom": nom,
            "commune": commune,
            "code_postal": cp,
            "latitude": None,
            "longitude": None,
            "label": label_nom,
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

    source_def attendu :
        {
          "nom": "saveurs_en_or",
          "url": "https://...",
          "config": {...}   # optionnel — si absent, scrape_auto
        }
    """
    nom = source_def.get("nom", "inconnu")
    url = source_def.get("url")
    if not url:
        return []
    html = fetch_html(url)
    if not html:
        return []
    config = source_def.get("config") or {}
    if config:
        results = scrape_avec_config(html, config, nom)
        if results:
            return results
        # Fallback auto si la config n'a rien donné
    return scrape_auto(html, nom)
