"""Scraper Pages Jaunes — best-effort (anti-bot fréquent)."""

import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from config import TARGET_LAT, TARGET_LNG

logger = logging.getLogger(__name__)

BASE_URL = "https://www.pagesjaunes.fr"

SEARCH_TERMS = [
    "producteurs fermiers",
    "ferme",
    "fromagerie",
    "boulangerie artisanale",
    "apiculteur",
    "maraîcher",
    "brasserie artisanale",
    "savonnerie",
    "pépiniériste",
]


class PagesJaunesScraper(BaseScraper):

    @property
    def source_name(self) -> str:
        return "Pages Jaunes"

    def __init__(self):
        super().__init__()
        self.session.headers.update({
            "Referer": "https://www.pagesjaunes.fr/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "DNT": "1",
        })

    def scrape(self) -> list[dict]:
        logger.info(f"[{self.source_name}] Tentative de recherche (peut échouer à cause de l'anti-bot)...")
        all_records = []

        for term in SEARCH_TERMS:
            records = self._search_term(term)
            all_records.extend(records)
            if records:
                logger.info(f"[{self.source_name}] '{term}': {len(records)} résultats")

        logger.info(f"[{self.source_name}] Total : {len(all_records)} résultats")
        return all_records

    def _search_term(self, term: str) -> list[dict]:
        """Recherche un terme sur Pages Jaunes."""
        url = f"{BASE_URL}/annuaire/les-pieux-50340/{term.replace(' ', '-')}"
        resp = self._get(url)
        if not resp:
            return []

        # Détecter le blocage
        if resp.status_code == 403 or "captcha" in resp.text.lower():
            logger.warning(f"[{self.source_name}] Bloqué (403 / captcha) pour '{term}'")
            return []

        return self._parse_results(resp.text)

    def _parse_results(self, html: str) -> list[dict]:
        """Parse les résultats de recherche."""
        soup = BeautifulSoup(html, "lxml")
        records = []
        today = date.today().isoformat()

        # Sélecteurs pour les cartes de résultats PJ
        cards = soup.select(".bi-bloc, .bi, .pj-list-item, [class*='bi-']")
        if not cards:
            cards = soup.select("li.bi")

        for card in cards:
            rec = self._empty_record()

            # Nom
            name_el = card.select_one(".bi-denomination, .denomination, h3 a, .bi-name")
            if name_el:
                rec["nom"] = name_el.get_text(strip=True)

            # Adresse
            addr_el = card.select_one(".bi-address, .address, .bi-adresse")
            if addr_el:
                addr_text = addr_el.get_text(" ", strip=True)
                rec["ville"] = self._extract_city(addr_text)
                postal = re.search(r"\b(\d{5})\b", addr_text)
                if postal:
                    rec["code_postal"] = postal.group(1)

            # Téléphone
            phone_el = card.select_one(".bi-phone, .phone, [class*='phone']")
            if phone_el:
                rec["telephone"] = phone_el.get_text(strip=True)

            # Catégorie / activité
            activity_el = card.select_one(".bi-activity, .activity, .bi-activite")
            if activity_el:
                rec["produits"] = activity_el.get_text(strip=True)

            # Lien
            link = card.select_one("a.bi-denomination, a.denomination, h3 a")
            if link:
                href = link.get("href", "")
                if href.startswith("/"):
                    rec["site_web"] = BASE_URL + href

            rec["source"] = self.source_name
            rec["date_collecte"] = today

            if rec["nom"]:
                records.append(rec)

        return records

    @staticmethod
    def _extract_city(address: str) -> str:
        """Extrait la ville d'une adresse PJ."""
        # Format typique : "12 rue Machin 50340 Les Pieux"
        match = re.search(r"\d{5}\s+(.+)$", address)
        if match:
            return match.group(1).strip()
        return ""


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = PagesJaunesScraper()
    results = scraper.scrape()
    for r in results[:5]:
        print(f"  {r['nom']} — {r['ville']} — {r['telephone']}")
