"""Scraper AcheterALaSource.com."""

import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

BASE_URL = "https://www.acheteralasource.com"


class AcheterALaSourceScraper(BaseScraper):

    @property
    def source_name(self) -> str:
        return "Acheter à la Source"

    def scrape(self) -> list[dict]:
        logger.info(f"[{self.source_name}] Recherche des producteurs dans la Manche (50)...")
        all_records = []

        page = 1
        max_pages = 10

        while page <= max_pages:
            url = f"{BASE_URL}/producteurs-en-france/all/departement/50/page/{page}"
            resp = self._get(url)
            if not resp:
                break

            records = self._parse_page(resp.text)
            if not records:
                break

            all_records.extend(records)
            logger.info(f"[{self.source_name}] Page {page}: {len(records)} résultats")
            page += 1

        logger.info(f"[{self.source_name}] Total : {len(all_records)} producteurs")
        return all_records

    def _parse_page(self, html: str) -> list[dict]:
        """Parse une page de résultats."""
        soup = BeautifulSoup(html, "lxml")
        records = []
        today = date.today().isoformat()

        # Chercher les blocs producteurs
        cards = soup.select(
            ".producteur, .card, .listing-item, article, "
            "[class*='producer'], [class*='producteur'], .item"
        )

        for card in cards:
            rec = self._empty_record()

            # Nom
            name_el = card.select_one("h2, h3, h4, .name, .title, a[title]")
            if name_el:
                rec["nom"] = name_el.get_text(strip=True)

            # Lieu
            loc_el = card.select_one(".location, .ville, .city, .address, small")
            if loc_el:
                text = loc_el.get_text(strip=True)
                postal = re.search(r"\b(\d{5})\b", text)
                if postal:
                    rec["code_postal"] = postal.group(1)
                    # Ville = ce qui suit le code postal
                    city_match = re.search(r"\d{5}\s*(.+)", text)
                    if city_match:
                        rec["ville"] = city_match.group(1).strip()
                else:
                    rec["ville"] = text

            # Produits / description
            desc_el = card.select_one(".description, .products, p, .details")
            if desc_el:
                rec["produits"] = desc_el.get_text(strip=True)[:200]

            # Lien
            link = card.select_one("a[href]")
            if link:
                href = link.get("href", "")
                if href and not href.startswith("http"):
                    href = BASE_URL + href
                rec["site_web"] = href

            rec["source"] = self.source_name
            rec["date_collecte"] = today

            if rec["nom"] and len(rec["nom"]) > 2:
                records.append(rec)

        return records


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = AcheterALaSourceScraper()
    results = scraper.scrape()
    for r in results[:5]:
        print(f"  {r['nom']} — {r['ville']} — {r['produits'][:60]}")
