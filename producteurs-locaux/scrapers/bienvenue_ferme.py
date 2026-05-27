"""Scraper Bienvenue à la Ferme — source principale.

Le site utilise SvelteKit et embarque les données JSON de recherche dans le HTML.
On pagine la recherche, puis on scrape chaque fiche producteur pour récupérer
les détails (téléphone, email, réseaux sociaux, labels, produits en français).
"""

import html
import json
import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from config import TARGET_LAT, TARGET_LNG, RADIUS_KM

logger = logging.getLogger(__name__)

BASE_URL = "https://www.bienvenue-a-la-ferme.com"
SEARCH_URL = f"{BASE_URL}/recherche"

# Mapping type anglais -> français pour les URLs
TYPE_MAP = {"farm": "ferme", "market": "marche"}


class BienvenueFermeScraper(BaseScraper):

    @property
    def source_name(self) -> str:
        return "Bienvenue à la Ferme"

    def scrape(self) -> list[dict]:
        logger.info(f"[{self.source_name}] Parcours du répertoire Normandie...")
        all_records = []

        # Stratégie : parcourir le répertoire régional BAF (fiable)
        # puis scraper chaque fiche producteur de la Manche
        farm_urls = self._collect_farm_urls()
        logger.info(f"[{self.source_name}] {len(farm_urls)} fermes dans la Manche trouvées")

        if not farm_urls:
            logger.warning(f"[{self.source_name}] Aucune ferme trouvée via le répertoire")
            return []

        # Scraper chaque fiche producteur
        today = date.today().isoformat()
        for i, url in enumerate(farm_urls):
            rec = self._empty_record()
            rec["source"] = self.source_name
            rec["date_collecte"] = today
            rec["site_web"] = f"{BASE_URL}{url}"

            # Extraire nom et ville depuis l'URL
            # Format: /normandie/manche/VILLE/ferme/NOM-SLUG/ID
            parts = url.strip("/").split("/")
            if len(parts) >= 5:
                rec["ville"] = parts[2].replace("-", " ").title()
                rec["nom"] = parts[4].replace("-", " ").title()

            # Scraper la page de détail
            detail = self._scrape_detail(rec["site_web"])
            if detail:
                self._merge_detail(rec, detail)
                # Nom depuis la page est plus fiable
                if detail.get("nom"):
                    rec["nom"] = detail["nom"]

            if rec["nom"]:
                all_records.append(rec)

            if (i + 1) % 10 == 0:
                logger.info(f"[{self.source_name}] {i + 1}/{len(farm_urls)} fiches traitées...")

        # Géocoder et filtrer par distance
        from utils.geocoding import geocode, calculate_distance
        filtered = []
        for rec in all_records:
            if not rec.get("latitude") or not rec.get("longitude"):
                cp = rec.get("code_postal", "")
                ville = rec.get("ville", "")
                if cp or ville:
                    coords = geocode(f"{ville} {cp}, France")
                    if coords:
                        rec["latitude"], rec["longitude"] = coords

            if rec.get("latitude") and rec.get("longitude"):
                try:
                    dist = calculate_distance(float(rec["latitude"]), float(rec["longitude"]))
                    rec["distance_km"] = dist
                    if dist <= RADIUS_KM:
                        filtered.append(rec)
                    continue
                except (ValueError, TypeError):
                    pass
            # Pas de coordonnées — géocoder via ville + CP
            cp = rec.get("code_postal", "")
            ville = rec.get("ville", "")
            if cp or ville:
                coords = geocode(f"{ville} {cp}, France")
                if coords:
                    rec["latitude"], rec["longitude"] = coords
                    dist = calculate_distance(coords[0], coords[1])
                    rec["distance_km"] = dist
                    if dist <= RADIUS_KM:
                        filtered.append(rec)
                    continue
            # Vraiment pas de données — garder par défaut
            filtered.append(rec)

        logger.info(f"[{self.source_name}] {len(filtered)}/{len(all_records)} producteurs dans le rayon de {RADIUS_KM}km")
        return filtered

    def _collect_farm_urls(self) -> list[str]:
        """Collecte les URLs des fermes de la Manche via le répertoire Normandie."""
        manche_urls = set()
        empty_pages = 0
        for page in range(1, 50):
            url = f"{BASE_URL}/normandie/recherche"
            resp = self._get(url, params={"categories[]": "products", "page": page})
            if not resp:
                break
            # Extraire TOUS les liens ferme (pour vérifier la fin de pagination)
            all_links = re.findall(r'href="(/normandie/[^"]*?/ferme/[^"?]+)', resp.text)
            if not all_links:
                empty_pages += 1
                if empty_pages >= 2:
                    break
                continue
            empty_pages = 0
            # Garder seulement les fermes de la Manche
            for link in all_links:
                if "/manche/" in link:
                    manche_urls.add(link)
            if page % 10 == 0:
                logger.info(f"[{self.source_name}] Page {page}: {len(manche_urls)} fermes Manche")
        return list(manche_urls)

    # ── Parsing de la page de recherche ──

    def _parse_search_page(self, page_html: str) -> list[dict]:
        """Extrait les items depuis le JSON SvelteKit embarqué."""
        items = []
        markers = {}

        body_matches = re.findall(r'"body"\s*:\s*"(\{.*?\})"', page_html)
        for body_str in body_matches:
            try:
                body_str = body_str.replace('\\"', '"').replace('\\\\', '\\')
                body = json.loads(body_str)
                results = body.get("results", {})
                if isinstance(results, dict) and "items" in results:
                    items = results["items"]
                    # Extraire coordonnées des markers
                    map_data = body.get("map", {})
                    if isinstance(map_data, dict):
                        for marker in map_data.get("markers", []):
                            producer = marker.get("producer", {})
                            if producer and "bafId" in producer:
                                loc = marker.get("location", {})
                                markers[producer["bafId"]] = (loc.get("lat"), loc.get("lon"))
                    break
            except (json.JSONDecodeError, ValueError):
                continue

        if not items:
            return []

        records = []
        today = date.today().isoformat()

        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "market":
                continue

            rec = self._empty_record()
            rec["nom"] = item.get("name", "")
            rec["source"] = self.source_name
            rec["date_collecte"] = today

            # Contact
            contact = item.get("contact", {})
            if isinstance(contact, dict):
                first = contact.get("firstName", "")
                last = contact.get("lastName", "")
                if first or last:
                    rec["raison_sociale"] = f"{first} {last}".strip()

            # Adresse
            addr = item.get("address", {})
            if isinstance(addr, dict):
                rec["ville"] = addr.get("city", "")
                rec["code_postal"] = str(addr.get("postalCode", ""))

            # URL — construction correcte avec type français
            url_data = item.get("url", {})
            if isinstance(url_data, dict):
                url_type = TYPE_MAP.get(url_data.get("type", ""), "ferme")
                parts = [
                    url_data.get("region", ""),
                    url_data.get("department", ""),
                    url_data.get("city", ""),
                    url_type,
                    url_data.get("slugName", ""),
                    str(url_data.get("id", "")),
                ]
                if all(parts):
                    rec["site_web"] = f"{BASE_URL}/{'/'.join(parts)}"

            # Description — nettoyer les entités HTML
            desc = item.get("description", "")
            if desc:
                desc = html.unescape(str(desc))
                desc = re.sub(r'<[^>]+>', ' ', desc)  # Retirer balises HTML
                desc = re.sub(r'\s+', ' ', desc).strip()
                rec["produits"] = desc[:300]

            # Coordonnées depuis les markers
            baf_id = item.get("bafId")
            if baf_id and baf_id in markers:
                rec["latitude"], rec["longitude"] = markers[baf_id]

            if rec["nom"]:
                records.append(rec)

        return records

    # ── Scraping de la page de détail ──

    def _scrape_detail(self, url: str) -> dict | None:
        """Scrape une fiche producteur pour les infos détaillées."""
        resp = self._get(url)
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        detail = {}

        # Nom du producteur depuis og:title (le plus fiable)
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title:
            title_text = og_title.get("content", "")
            # Format: "Nom - Ville - Département - Bienvenue à la ferme"
            parts = title_text.split(" - ")
            if parts:
                detail["nom"] = parts[0].strip()
            if len(parts) >= 2:
                detail["ville"] = parts[1].strip()
            if len(parts) >= 3:
                # Extraire le département (ex: "Manche (50)")
                dept_match = re.search(r'\((\d{2})\)', parts[2])
                if dept_match:
                    dept = dept_match.group(1)
                    # Chercher le code postal complet dans le texte
                    addr_text = soup.get_text(" ", strip=True)
                    cp_match = re.search(rf'\b({dept}\d{{3}})\b', addr_text)
                    if cp_match:
                        detail["code_postal"] = cp_match.group(1)
        if not detail.get("nom"):
            # Fallback: second h1 (le premier est le titre générique BAF)
            h1_tags = soup.find_all("h1")
            if len(h1_tags) >= 2:
                detail["nom"] = h1_tags[1].get_text(strip=True)
            elif h1_tags:
                text = h1_tags[0].get_text(strip=True)
                if "Produits fermiers" not in text:
                    detail["nom"] = text

        # Téléphone — chercher les liens tel:
        phones = []
        for a in soup.select('a[href^="tel:"]'):
            phone = a.get("href", "").replace("tel:", "").strip()
            if phone and phone not in phones:
                phones.append(phone)
        if not phones:
            # Fallback : regex sur tout le texte
            for match in re.findall(r'(?:0[1-9])[\s.\-]?(?:\d{2}[\s.\-]?){4}', soup.get_text()):
                phone = re.sub(r'[\s.\-]', '', match)
                if phone not in phones:
                    phones.append(phone)
        if phones:
            detail["telephone"] = " / ".join(phones[:2])

        # Email — chercher les liens mailto:
        for a in soup.select('a[href^="mailto:"]'):
            email = a.get("href", "").replace("mailto:", "").strip()
            if email and "@" in email:
                detail["email"] = email
                break
        if "email" not in detail:
            # Regex fallback
            emails = re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', soup.get_text())
            if emails:
                detail["email"] = emails[0]

        # Réseaux sociaux
        socials = []
        for a in soup.select('a[href]'):
            href = a.get("href", "")
            for domain in ["facebook.com", "instagram.com", "twitter.com", "youtube.com", "linkedin.com", "tiktok.com"]:
                if domain in href:
                    socials.append(href)
                    break
        if socials:
            detail["reseaux_sociaux"] = " | ".join(dict.fromkeys(socials))  # dédoublonner

        # Site web externe (pas bienvenue-a-la-ferme)
        for a in soup.select('a[href^="http"]'):
            href = a.get("href", "")
            if href and "bienvenue-a-la-ferme" not in href and "facebook.com" not in href and "instagram.com" not in href:
                text = a.get_text(strip=True).lower()
                if any(w in text for w in ["site", "boutique", "shop", "www", "visiter"]) or any(w in href for w in [".fr", ".com"]):
                    detail["site_externe"] = href
                    break

        # Labels / Certifications
        labels = []
        text_full = soup.get_text(" ", strip=True).lower()
        label_keywords = {
            "Agriculture biologique": ["agriculture biologique", "certifié bio", "label bio", "ab "],
            "Bio": ["bio", "biologique"],
            "AOP": ["aop", "appellation d'origine"],
            "AOC": ["aoc", "appellation d'origine contrôlée"],
            "IGP": ["igp", "indication géographique"],
            "Label Rouge": ["label rouge"],
            "Bleu-Blanc-Cœur": ["bleu-blanc-cœur", "bleu blanc coeur"],
            "HVE": ["hve", "haute valeur environnementale"],
            "Fermier": ["fermier", "produit fermier"],
        }
        for label, keywords in label_keywords.items():
            for kw in keywords:
                if kw in text_full:
                    labels.append(label)
                    break
        if labels:
            detail["labels"] = ", ".join(labels)

        # Produits en français — chercher les sections produits
        produits_textes = []

        # Chercher les éléments qui décrivent les produits
        for selector in [
            "h2, h3, h4",  # Titres de sections
            ".product, .produit, [class*='product'], [class*='produit']",
            "li",
        ]:
            for el in soup.select(selector):
                text = el.get_text(strip=True)
                # Filtrer les textes pertinents (produits alimentaires / artisanaux)
                text_lower = text.lower()
                if any(kw in text_lower for kw in [
                    "fromage", "lait", "beurre", "crème", "cidre", "jus", "calvados",
                    "viande", "porc", "bœuf", "boeuf", "agneau", "volaille", "poulet",
                    "légume", "fruit", "pomme", "carotte", "tomate", "salade",
                    "miel", "confiture", "pain", "farine", "huître", "moule",
                    "bière", "vin", "poiré", "pommeau", "eau-de-vie",
                    "savon", "cosmétique", "poterie", "artisan",
                    "œuf", "oeuf", "canard", "oie", "charcuterie", "saucisson",
                    "conserve", "terrine", "rillette", "pâté",
                    "sel", "herbe", "aromate", "épice",
                ]) and 5 < len(text) < 200:
                    produits_textes.append(text)

        if produits_textes:
            # Prendre les textes uniques les plus pertinents
            unique_produits = list(dict.fromkeys(produits_textes))[:8]
            produits_str = ", ".join(unique_produits)
            # Nettoyer les artefacts de page (coordonnées mélangées, etc.)
            produits_str = re.sub(r'Nos coordonnées.*', '', produits_str)
            produits_str = re.sub(r'Contact[A-Z].*', '', produits_str)
            produits_str = re.sub(r'Adresse\d+.*', '', produits_str)
            produits_str = re.sub(r'Courriel\S+', '', produits_str)
            produits_str = re.sub(r'\s{2,}', ' ', produits_str).strip().rstrip(",. ")
            if produits_str:
                detail["produits"] = produits_str

        return detail if detail else None

    def _merge_detail(self, rec: dict, detail: dict):
        """Fusionne les données de détail dans l'enregistrement."""
        # Nom, ville, code postal : préférer les données de la page de détail
        for key in ["nom", "ville", "code_postal"]:
            if detail.get(key):
                rec[key] = detail[key]
        for key in ["telephone", "email", "labels"]:
            if detail.get(key) and not rec.get(key):
                rec[key] = detail[key]

        # Réseaux sociaux : fusionner (vrais liens Facebook/Instagram du producteur)
        if detail.get("reseaux_sociaux"):
            existing = rec.get("reseaux_sociaux", "")
            # Filtrer les liens génériques BAF
            new_socials = detail["reseaux_sociaux"]
            if "bienvenuealaferme" not in new_socials:
                rec["reseaux_sociaux"] = new_socials
            elif not existing:
                rec["reseaux_sociaux"] = new_socials

        # Produits : remplacer si le détail est plus riche et en français
        if detail.get("produits"):
            current = rec.get("produits", "")
            if not current or current == "products" or len(detail["produits"]) > len(current):
                rec["produits"] = detail["produits"]

        # Site web : garder l'URL BAF comme fiche, et le site externe comme vrai site
        baf_url = rec.get("site_web", "")
        if detail.get("site_externe"):
            ext_url = detail["site_externe"]
            # Exclure Google Maps comme site web
            if "maps.google" not in ext_url and "google.com/maps" not in ext_url:
                rec["site_web"] = ext_url
            # Ajouter la fiche BAF dans les réseaux sociaux si pas déjà présente
            if baf_url and baf_url not in rec.get("reseaux_sociaux", ""):
                current_rs = rec.get("reseaux_sociaux", "")
                rec["reseaux_sociaux"] = f"{current_rs} | {baf_url}".strip(" |") if current_rs else baf_url

    # ── Filtrage ──

    @staticmethod
    def _is_local(rec: dict) -> bool:
        """Vérifie si le producteur est dans les départements du Grand Ouest."""
        cp = str(rec.get("code_postal", ""))
        if len(cp) < 2:
            return False
        # Normandie : Manche (50), Calvados (14), Orne (61), Eure (27), Seine-Maritime (76)
        # Bretagne proche : Côtes-d'Armor (22), Ille-et-Vilaine (35), Finistère (29), Morbihan (56)
        # Pays de la Loire : Mayenne (53), Sarthe (72), Loire-Atlantique (44)
        return cp[:2] in ("50", "14", "61", "27", "76", "22", "35", "29", "56", "53", "72", "44")

    @staticmethod
    def _within_radius(rec: dict) -> bool:
        """Vérifie si le producteur est dans le rayon."""
        from utils.geocoding import calculate_distance
        lat = rec.get("latitude")
        lng = rec.get("longitude")
        if lat and lng:
            try:
                return calculate_distance(float(lat), float(lng)) <= RADIUS_KM
            except (ValueError, TypeError):
                return True
        return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = BienvenueFermeScraper()
    results = scraper.scrape()
    print(f"\n{len(results)} producteurs trouvés :")
    for r in results[:10]:
        print(f"  {r['nom']} — {r['ville']} — Tel: {r.get('telephone', '-')} — {r.get('produits', '')[:80]}")
