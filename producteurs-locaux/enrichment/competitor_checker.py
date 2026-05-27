"""Vérification de la présence des producteurs chez les concurrents.

Stratégie : pour chaque producteur, on recherche son nom sur les sites
des concurrents (drives/catalogues en ligne) via une requête HTTP directe
sur les pages de recherche de chaque enseigne.
"""

import logging
import re
import time

import requests

from config import USER_AGENT, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)

# Sites à interroger pour chaque concurrent
COMPETITOR_SEARCH = {
    "Leclerc Equeurdreville": [
        # Recherche sur le drive Leclerc
        "https://www.leclercdrive.fr/recherche?q={query}",
        # Catalogue en ligne E.Leclerc
        "https://www.e.leclerc/recherche?q={query}",
    ],
    "Intermarché Les Pieux": [
        # Drive Intermarché
        "https://www.intermarche.com/recherche?q={query}",
    ],
}


def check_competitors(records: list[dict]):
    """Vérifie la présence de chaque producteur chez les concurrents.

    Tente une recherche sur les sites drives des concurrents.
    Si la recherche échoue (sites JS-only, 403, etc.), marque 'Inconnu'.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })

    # Tester si les sites concurrents sont accessibles
    accessible = {}
    for competitor, urls in COMPETITOR_SEARCH.items():
        accessible[competitor] = False
        for url_template in urls:
            test_url = url_template.format(query="fromage")
            try:
                resp = session.get(test_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                if resp.status_code == 200 and len(resp.text) > 500:
                    accessible[competitor] = url_template
                    logger.info(f"[Concurrence] {competitor} : site accessible ({url_template})")
                    break
            except requests.RequestException:
                continue

    if not any(accessible.values()):
        logger.warning("[Concurrence] Aucun site concurrent accessible — toutes les colonnes à 'Inconnu'")

    for rec in records:
        nom = rec.get("nom", "")
        rec["vu_chez_leclerc_equeurdreville"] = "Inconnu"
        rec["vu_chez_intermarche_les_pieux"] = "Inconnu"

        if not nom:
            continue

        for competitor, url_template in accessible.items():
            if not url_template:
                continue

            col_key = _col_key(competitor)
            # Rechercher le nom du producteur
            search_url = url_template.format(query=requests.utils.quote(nom))
            try:
                time.sleep(1)
                resp = session.get(search_url, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    text = resp.text.lower()
                    nom_lower = nom.lower()
                    # Vérifier si le nom apparaît dans les résultats
                    if nom_lower in text:
                        rec[col_key] = "Oui"
                    else:
                        # Chercher des fragments du nom (ex: "Ferme de la Lande" -> "lande")
                        name_parts = [p for p in re.split(r'[\s\-/]+', nom_lower) if len(p) > 3
                                      and p not in ("ferme", "earl", "gaec", "sarl", "maison")]
                        if name_parts and any(part in text for part in name_parts):
                            rec[col_key] = "Possible"
                        else:
                            rec[col_key] = "Non"
            except requests.RequestException:
                pass

    found_leclerc = sum(1 for r in records if r.get("vu_chez_leclerc_equeurdreville") == "Oui")
    found_inter = sum(1 for r in records if r.get("vu_chez_intermarche_les_pieux") == "Oui")
    logger.info(f"[Concurrence] Résultat : {found_leclerc} chez Leclerc, {found_inter} chez Intermarché")


def _col_key(competitor_name: str) -> str:
    """Génère la clé de colonne pour un concurrent."""
    name = competitor_name.lower()
    if "leclerc" in name:
        return "vu_chez_leclerc_equeurdreville"
    elif "intermarche" in name or "intermarché" in name:
        return "vu_chez_intermarche_les_pieux"
    return f"vu_chez_{name.replace(' ', '_')}"
