"""Scraper Locavor.fr — producteurs en circuit court."""

import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from config import RADIUS_KM

logger = logging.getLogger(__name__)

# Traduction des slugs catégories Locavor
SLUG_TRANSLATIONS = {
    "poissons-mollusques-crustaces": "Poissons, mollusques, crustacés",
    "boissons-alcoolisees": "Boissons alcoolisées",
    "cafe-the-infusions": "Café, thé, infusions",
    "condiments-huiles-epices": "Condiments, huiles, épices",
    "plantes-aromatiques-medicinales": "Plantes aromatiques et médicinales",
    "cereales-farines-graines": "Céréales, farines, graines",
    "fruits-legumes": "Fruits et légumes",
    "produits-laitiers": "Produits laitiers",
    "viandes-charcuterie": "Viandes et charcuterie",
    "oeufs": "Œufs",
    "miel-confitures": "Miel et confitures",
    "pain-patisserie": "Pain et pâtisserie",
    "jus-sirops": "Jus et sirops",
    "cidre-poire": "Cidre et poiré",
    "biere": "Bière",
    "conserves-plats-cuisines": "Conserves et plats cuisinés",
    "cosmetiques-savons": "Cosmétiques et savons",
    "artisanat": "Artisanat",
}

BASE_URL = "https://locavor.fr"
SEARCH_URL = f"{BASE_URL}/annuaire-producteurs-en-circuit-court"


class LocavorScraper(BaseScraper):

    @property
    def source_name(self) -> str:
        return "Locavor"

    def scrape(self) -> list[dict]:
        logger.info(f"[{self.source_name}] Recherche de producteurs en circuit court...")
        all_records = []

        # Rechercher depuis plusieurs villes proches pour couvrir tout le Cotentin
        search_cities = [
            "Les Pieux-50340",
            "Cherbourg-en-Cotentin-50100",
            "Valognes-50700",
            "Barneville-Carteret-50270",
            "Bricquebec-50260",
            "Saint-Sauveur-le-Vicomte-50390",
            "Beaumont-Hague-50440",
        ]

        for city in search_cities:
            for query_type in ["producteur", "artisan"]:
                params = {"q": city, "m": query_type}
                resp = self._get(SEARCH_URL, params=params)
                if not resp:
                    continue
                records = self._parse_results(resp.text)
                all_records.extend(records)
            logger.info(f"[{self.source_name}] Recherche '{city}': {len(all_records)} total")

        dept_url = f"{BASE_URL}/annuaire-producteurs-en-circuit-court/departement/50-manche"
        resp = self._get(dept_url)
        if resp:
            records = self._parse_results(resp.text)
            all_records.extend(records)
            logger.info(f"[{self.source_name}] Département Manche: {len(records)} résultats")

        # Dédoublonner avant de scraper les détails
        seen_urls = set()
        unique_records = []
        for rec in all_records:
            url = rec.get("_detail_url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_records.append(rec)
            elif not url:
                unique_records.append(rec)
        logger.info(f"[{self.source_name}] {len(unique_records)} producteurs uniques (avant détails)")
        all_records = unique_records

        # Scraper les fiches détail pour enrichir (adresse, coordonnées)
        enriched = 0
        for rec in all_records:
            url = rec.get("_detail_url", "")
            if url:
                detail = self._scrape_detail(url)
                if detail:
                    # Écraser les données de détail plus précises
                    for key in ["latitude", "longitude", "code_postal", "ville"]:
                        if detail.get(key):
                            rec[key] = detail[key]
                    # Site web externe (préférer au lien Locavor)
                    if detail.get("site_externe"):
                        rec["site_web"] = detail["site_externe"]
                    # Description en français (remplace les slugs)
                    if detail.get("description"):
                        rec["produits"] = detail["description"]
                    # Ajouter les champs manquants
                    for key, val in detail.items():
                        if key not in ("site_externe", "description") and val and not rec.get(key):
                            rec[key] = val
                    enriched += 1
            # Nettoyer le champ temporaire
            rec.pop("_detail_url", None)

        # Nettoyer les produits (traduire les slugs)
        for rec in all_records:
            self._clean_products(rec)

        # Filtrer par distance
        from utils.geocoding import calculate_distance
        filtered = []
        for rec in all_records:
            if rec.get("latitude") and rec.get("longitude"):
                try:
                    dist = calculate_distance(float(rec["latitude"]), float(rec["longitude"]))
                    rec["distance_km"] = dist
                    if dist <= RADIUS_KM:
                        filtered.append(rec)
                    continue
                except (ValueError, TypeError):
                    pass
            # Pas de coords — garder si le CP est dans le 50
            cp = str(rec.get("code_postal", ""))
            if cp.startswith("50"):
                filtered.append(rec)

        logger.info(f"[{self.source_name}] Total : {len(filtered)}/{len(all_records)} producteurs dans le rayon ({enriched} enrichis)")
        return filtered

    @staticmethod
    def _clean_products(rec: dict):
        """Nettoie la colonne produits : traduit les slugs Locavor en français."""
        produits = rec.get("produits", "")
        if not produits:
            return
        # Retirer le nom du producteur s'il est en début de chaîne
        nom = rec.get("nom", "")
        if nom and produits.startswith(nom):
            produits = produits[len(nom):].lstrip(", ")
        # Traduire les slugs
        parts = [p.strip() for p in produits.split(",")]
        translated = []
        for part in parts:
            part = part.strip()
            if part in SLUG_TRANSLATIONS:
                translated.append(SLUG_TRANSLATIONS[part])
            elif "-" in part and part == part.lower():
                # Slug non reconnu — convertir les tirets en espaces, capitaliser
                translated.append(part.replace("-", " ").capitalize())
            elif part:
                translated.append(part)
        rec["produits"] = ", ".join(translated) if translated else ""

    def _parse_results(self, page_html: str) -> list[dict]:
        """Parse les résultats de recherche Locavor.

        Structure réelle : liens <a href="/presentation/ID-slug">
        contenant "Xkm - Ville\nNom du producteur"
        """
        soup = BeautifulSoup(page_html, "lxml")
        records = []
        today = date.today().isoformat()

        # Les résultats sont des liens vers /presentation/
        links = soup.find_all("a", href=re.compile(r"/presentation/\d+"))

        for link in links:
            text = link.get_text(" | ", strip=True)
            href = link.get("href", "")

            # Parser le texte : "8km - Teurthéville-Hague | Phytotempo"
            # ou "52km - Agon-Coutainville | Les Huîtres Du Père Gus"
            match = re.match(r"(\d+)km\s*-\s*(.+?)\s*\|\s*(.+)", text)
            if not match:
                # Fallback : juste le nom
                match = re.match(r"(?:(\d+)km\s*-\s*(.+?))?\s*(.+)", text)
            if not match:
                continue

            rec = self._empty_record()
            groups = match.groups()

            if groups[0]:
                try:
                    rec["distance_km"] = float(groups[0])
                except ValueError:
                    pass
            if groups[1]:
                ville = groups[1].strip().rstrip(".")
                if ville.endswith("..."):
                    ville = ville[:-3]
                rec["ville"] = ville
            if groups[2]:
                rec["nom"] = groups[2].strip()

            # Catégories depuis les images alt dans le voisinage
            parent = link.parent
            if parent:
                imgs = parent.find_all("img", alt=True)
                product_texts = [img.get("alt", "") for img in imgs
                                 if img.get("alt") and "logo" not in img.get("alt", "").lower()]
                if product_texts:
                    rec["produits"] = ", ".join(product_texts)

            # URL détail
            if not href.startswith("http"):
                href = BASE_URL + href
            rec["_detail_url"] = href
            rec["site_web"] = href

            rec["source"] = self.source_name
            rec["date_collecte"] = today

            if rec["nom"] and len(rec["nom"]) > 2:
                records.append(rec)

        return records

    def _scrape_detail(self, url: str) -> dict | None:
        """Scrape une fiche producteur Locavor pour les détails.

        Utilise les microdonnées Schema.org (itemprop) pour l'adresse,
        les coordonnées GPS et la description.
        """
        resp = self._get(url)
        if not resp:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        detail = {}

        # Adresse via Schema.org microdata (fiable)
        cp_el = soup.find(attrs={"itemprop": "postalCode"})
        if cp_el:
            detail["code_postal"] = cp_el.get_text(strip=True)
        city_el = soup.find(attrs={"itemprop": "addressLocality"})
        if city_el:
            detail["ville"] = city_el.get_text(strip=True)

        # Coordonnées GPS via Schema.org geo microdata (meta tags)
        geo = soup.find(attrs={"itemprop": "geo"})
        if geo:
            lat_el = geo.find(attrs={"itemprop": "latitude"})
            lng_el = geo.find(attrs={"itemprop": "longitude"})
            if lat_el and lng_el:
                try:
                    lat = float(lat_el.get("content", ""))
                    lng = float(lng_el.get("content", ""))
                    if 40 < lat < 55 and -5 < lng < 10:
                        detail["latitude"] = lat
                        detail["longitude"] = lng
                except (ValueError, TypeError):
                    pass

        # Fallback : coordonnées depuis le lien Google Maps
        if "latitude" not in detail:
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                if "maps" in href or "google" in href:
                    coords_match = re.search(r'([\d.-]+),([\d.-]+)', href)
                    if coords_match:
                        try:
                            lat = float(coords_match.group(1))
                            lng = float(coords_match.group(2))
                            if 40 < lat < 55 and -5 < lng < 10:
                                detail["latitude"] = lat
                                detail["longitude"] = lng
                        except ValueError:
                            pass
                    break

        # Description en français via Schema.org
        desc_el = soup.find(attrs={"itemprop": "description"})
        if desc_el:
            desc = desc_el.get_text(strip=True)
            if desc and len(desc) > 10:
                detail["description"] = desc[:300]

        # Lien vers le site web externe du producteur
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if (href.startswith("http") and "locavor.fr" not in href
                    and "google" not in href and "facebook" not in href
                    and "instagram" not in href):
                detail["site_externe"] = href
                break

        return detail if detail else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = LocavorScraper()
    results = scraper.scrape()
    for r in results[:5]:
        print(f"  {r['nom']} — {r['ville']} — {r.get('produits', '')[:80]}")
