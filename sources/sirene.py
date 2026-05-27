"""API recherche-entreprises (annuaire.entreprise.gouv.fr / api.gouv.fr).
Pas de clé, ~7 req/s. Doc : https://recherche-entreprises.api.gouv.fr/docs/
"""
from __future__ import annotations
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Callable
import requests

BASE = "https://recherche-entreprises.api.gouv.fr/search"
SLEEP = 0.18  # ~5.5 req/s, marge sous le quota
MAX_PAGES_PAR_CODE = 8     # plafond par défaut (transformateurs)
MAX_PAGES_AGRICOLE = 20    # codes 01.xx : plafond plus large car beaucoup de petits producteurs légitimes
N_WORKERS = 4  # parallélisme modéré (4 × 0.18s = 22 req/s en pointe ; quota géré par lock global)

# Lock pour respecter le quota global même en parallèle
_request_lock = threading.Lock()
_last_request_ts = [0.0]


def _throttle():
    """Garantit ~SLEEP secondes entre 2 requêtes à l'échelle globale (tous threads)."""
    with _request_lock:
        elapsed = time.time() - _last_request_ts[0]
        if elapsed < SLEEP:
            time.sleep(SLEEP - elapsed)
        _last_request_ts[0] = time.time()


def _get(params: dict) -> dict:
    """Un appel API avec retry minimal et throttle global."""
    for tentative in range(3):
        try:
            _throttle()
            r = requests.get(BASE, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(1.0)
                continue
        except requests.RequestException:
            time.sleep(0.5)
    return {"results": [], "total_pages": 0, "total_results": 0}


def chercher(code_naf: str, departements: Iterable[str], per_page: int = 25) -> list[dict]:
    """Récupère tous les résultats pour (code_naf, departements). Pagine avec plafond."""
    deps = ",".join(departements)
    resultats = []
    page = 1
    max_pages = MAX_PAGES_AGRICOLE if code_naf.startswith("01.") else MAX_PAGES_PAR_CODE
    while page <= max_pages:
        params = {
            "activite_principale": code_naf,
            "departement": deps,
            "per_page": per_page,
            "page": page,
            "etat_administratif": "A",
        }
        data = _get(params)
        results = data.get("results", []) or []
        resultats.extend(results)
        total_pages = data.get("total_pages", 1)
        if page >= total_pages or not results:
            break
        page += 1
    return resultats


def chercher_multi(codes_naf: list[str], departements: list[str],
                   progress_cb: Callable[[int, int, str, int], None] | None = None) -> list[dict]:
    """Boucle parallèle sur plusieurs codes NAF, dédoublonne par SIREN.

    progress_cb(i, total, code, n_resultats) appelé à chaque code traité.
    """
    vus = set()
    out = []
    total = len(codes_naf)
    done = [0]
    lock = threading.Lock()

    def _job(code: str):
        results = chercher(code, departements)
        with lock:
            done[0] += 1
            n_added = 0
            for r in results:
                siren = r.get("siren")
                if siren and siren not in vus:
                    vus.add(siren)
                    out.append(r)
                    n_added += 1
            if progress_cb:
                progress_cb(done[0], total, code, n_added)
        return n_added

    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        list(as_completed([ex.submit(_job, c) for c in codes_naf]))
    return out


def extraire_normalise(r: dict) -> dict:
    """Réduit un résultat API à un dict propre et stable pour le pipeline."""
    siege = r.get("siege") or {}
    complements = r.get("complements") or {}
    dirigeants = r.get("dirigeants") or []
    dirigeant_principal = ""
    if dirigeants:
        d = dirigeants[0]
        nom = d.get("nom") or ""
        prenom = d.get("prenoms") or ""
        dirigeant_principal = f"{prenom} {nom}".strip()
        if not dirigeant_principal:
            dirigeant_principal = d.get("denomination") or ""
    return {
        "siren": r.get("siren"),
        "siret": siege.get("siret"),
        "nom_complet": r.get("nom_complet") or r.get("nom_raison_sociale") or "",
        "code_naf": r.get("activite_principale") or "",
        "libelle_naf": r.get("libelle_activite_principale") or "",
        "categorie_entreprise": r.get("categorie_entreprise") or "",
        "tranche_effectif": r.get("tranche_effectif_salarie") or "",
        "etat_administratif": r.get("etat_administratif") or "",
        "adresse": siege.get("adresse") or "",
        "commune": siege.get("libelle_commune") or "",
        "code_postal": siege.get("code_postal") or "",
        "latitude": siege.get("latitude"),
        "longitude": siege.get("longitude"),
        "est_bio": bool(complements.get("est_bio")),
        "est_patrimoine_vivant": bool(complements.get("est_patrimoine_vivant")),
        "est_societe_mission": bool(complements.get("est_societe_mission")),
        "est_ess": bool(complements.get("est_ess")),
        "est_entrepreneur_individuel": bool(complements.get("est_entrepreneur_individuel")),
        "dirigeant_principal": dirigeant_principal,
    }
