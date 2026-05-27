"""Scraper Google Places API (optionnel, nécessite une clé API)."""

import logging
from datetime import date

import requests

from scrapers.base import BaseScraper
from config import GOOGLE_API_KEY, TARGET_LAT, TARGET_LNG, RADIUS_KM

logger = logging.getLogger(__name__)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

QUERIES = [
    "producteur fermier",
    "ferme vente directe",
    "fromagerie artisanale",
    "maraîcher bio",
    "boulangerie artisanale",
    "brasserie artisanale cidre",
    "apiculteur miel",
    "ostréiculteur huîtres",
    "charcuterie artisanale",
    "savonnerie artisanale",
    "poterie artisanale",
    "pépinière horticulteur",
]


class GooglePlacesScraper(BaseScraper):

    @property
    def source_name(self) -> str:
        return "Google Places"

    def scrape(self) -> list[dict]:
        if not GOOGLE_API_KEY:
            logger.info(f"[{self.source_name}] Mode dégradé : pas de clé API Google Places (variable GOOGLE_PLACES_API_KEY)")
            return []

        logger.info(f"[{self.source_name}] Recherche avec {len(QUERIES)} requêtes...")
        all_records = []
        request_count = 0

        for query in QUERIES:
            records, count = self._search_query(query)
            all_records.extend(records)
            request_count += count

        logger.info(f"[{self.source_name}] {len(all_records)} résultats, {request_count} requêtes API effectuées")
        return all_records

    def _search_query(self, query: str) -> tuple[list[dict], int]:
        """Exécute une requête Text Search."""
        records = []
        request_count = 0

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_API_KEY,
            "X-Goog-FieldMask": (
                "places.displayName,places.formattedAddress,places.location,"
                "places.nationalPhoneNumber,places.websiteUri,places.types,"
                "places.primaryType,places.rating,nextPageToken"
            ),
        }

        body = {
            "textQuery": query,
            "locationBias": {
                "circle": {
                    "center": {"latitude": TARGET_LAT, "longitude": TARGET_LNG},
                    "radius": RADIUS_KM * 1000,  # en mètres
                }
            },
            "languageCode": "fr",
            "maxResultCount": 20,
        }

        page_token = None
        max_pages = 3

        for _ in range(max_pages):
            if page_token:
                body["pageToken"] = page_token

            self._rate_limit()
            try:
                resp = requests.post(SEARCH_URL, json=body, headers=headers, timeout=15)
                request_count += 1
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                logger.warning(f"[{self.source_name}] Erreur API pour '{query}': {e}")
                break

            places = data.get("places", [])
            if not places:
                break

            today = date.today().isoformat()
            for place in places:
                rec = self._empty_record()
                display = place.get("displayName", {})
                rec["nom"] = display.get("text", "") if isinstance(display, dict) else str(display)
                rec["produits"] = query  # On note la requête comme indication produit

                # Adresse
                addr = place.get("formattedAddress", "")
                rec["ville"] = self._extract_city(addr)
                rec["code_postal"] = self._extract_postal(addr)

                # Coordonnées
                loc = place.get("location", {})
                rec["latitude"] = loc.get("latitude")
                rec["longitude"] = loc.get("longitude")

                rec["telephone"] = place.get("nationalPhoneNumber", "")
                rec["site_web"] = place.get("websiteUri", "")
                rec["source"] = self.source_name
                rec["date_collecte"] = today

                if rec["nom"]:
                    records.append(rec)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return records, request_count

    @staticmethod
    def _extract_city(address: str) -> str:
        """Extrait la ville d'une adresse formatée."""
        parts = [p.strip() for p in address.split(",")]
        if len(parts) >= 2:
            # Avant-dernier élément souvent = "CODE VILLE"
            city_part = parts[-2].strip()
            # Retirer le code postal
            tokens = city_part.split()
            non_digits = [t for t in tokens if not t.isdigit()]
            return " ".join(non_digits)
        return ""

    @staticmethod
    def _extract_postal(address: str) -> str:
        """Extrait le code postal d'une adresse formatée."""
        import re
        match = re.search(r"\b(\d{5})\b", address)
        return match.group(1) if match else ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = GooglePlacesScraper()
    results = scraper.scrape()
    for r in results[:5]:
        print(f"  {r['nom']} — {r['ville']} — {r['telephone']}")
