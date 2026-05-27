"""Interface abstraite pour tous les scrapers."""

from abc import ABC, abstractmethod
import logging
import time

import requests

from config import USER_AGENT, REQUEST_DELAY, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    """Classe de base pour les scrapers de producteurs."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
        })
        self._last_request_time = 0

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Nom de la source (affiché dans la colonne 'Source')."""

    @abstractmethod
    def scrape(self) -> list[dict]:
        """Exécute le scraping et retourne une liste de producteurs.

        Chaque producteur est un dict avec les clés :
            nom, raison_sociale, categorie, sous_categorie, produits,
            ville, code_postal, latitude, longitude, telephone, email,
            site_web, reseaux_sociaux, labels, source, date_collecte
        """

    def _get(self, url: str, **kwargs) -> requests.Response | None:
        """GET avec rate limiting et gestion d'erreurs."""
        self._rate_limit()
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            logger.warning(f"[{self.source_name}] Erreur requête {url}: {e}")
            return None

    def _rate_limit(self):
        """Respecte le délai entre requêtes."""
        elapsed = time.time() - self._last_request_time
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self._last_request_time = time.time()

    @staticmethod
    def _empty_record() -> dict:
        """Retourne un enregistrement vide avec toutes les clés."""
        return {
            "nom": "",
            "raison_sociale": "",
            "categorie": "",
            "sous_categorie": "",
            "produits": "",
            "ville": "",
            "code_postal": "",
            "latitude": None,
            "longitude": None,
            "telephone": "",
            "email": "",
            "site_web": "",
            "reseaux_sociaux": "",
            "labels": "",
            "source": "",
            "date_collecte": "",
        }
