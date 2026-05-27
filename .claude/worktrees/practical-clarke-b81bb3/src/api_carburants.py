"""
api_carburants
==============

Client pour l'API publique `data.economie.gouv.fr` (dataset
`prix-des-carburants-en-france-flux-instantane-v2`).

En mode MOCK_API=true, lit une réponse mockée depuis un fichier JSON
(`tests/fixtures/reponse_api_exemple.json` par défaut). Utile en
DRY_RUN ou pour les tests.

En mode live, fait un GET HTTP et parse la réponse. Gère les erreurs
réseau proprement (API down → renvoie liste vide + log warning).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from src.moteur_decision import PrixCarburants

log = logging.getLogger(__name__)

API_BASE_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "prix-des-carburants-en-france-flux-instantane-v2/records"
)
# Limite raisonnable — on ne demande que quelques stations par requête
LIMIT_DEFAUT = 20


def recuperer_prix_stations(
    ids_stations: list[str],
    *,
    mock: bool = False,
    chemin_fixture: str | Path | None = None,
    timeout_s: int = 10,
) -> dict[str, PrixCarburants]:
    """Renvoie les prix actuels pour les stations demandées.

    Args:
        ids_stations: liste d'IDs prix-carburants (strings).
        mock: si True, lit depuis ``chemin_fixture`` au lieu d'appeler l'API.
        chemin_fixture: chemin vers JSON mocké (format résultat API).
        timeout_s: timeout HTTP.

    Returns:
        Dict ``{id_station: PrixCarburants}``. Stations introuvables = absentes
        du dict. Une station présente mais sans un carburant donné aura le
        champ correspondant à ``None`` dans :class:`PrixCarburants`.
    """
    if mock:
        records = _charger_fixture(chemin_fixture)
    else:
        records = _appeler_api(ids_stations, timeout_s)

    resultats: dict[str, PrixCarburants] = {}
    ids_set = set(ids_stations)
    for record in records:
        id_station = str(record.get("id", ""))
        if id_station not in ids_set:
            continue
        resultats[id_station] = _record_en_prix(record)

    manquantes = ids_set - set(resultats.keys())
    if manquantes:
        log.warning("Stations introuvables dans la réponse API : %s", sorted(manquantes))
    return resultats


def recuperer_prix_e85(
    ids_stations: list[str],
    *,
    mock: bool = False,
    chemin_fixture: str | Path | None = None,
    timeout_s: int = 10,
) -> dict[str, float | None]:
    """Comme recuperer_prix_stations mais ne renvoie que les prix E85.

    Pas dans :class:`PrixCarburants` car on ne pilote pas l'E85 (notre station
    ne le vend pas) — utilisé uniquement pour affichage dans l'onglet Concurrents.
    """
    if mock:
        records = _charger_fixture(chemin_fixture)
    else:
        records = _appeler_api(ids_stations, timeout_s)

    out: dict[str, float | None] = {}
    ids_set = set(ids_stations)
    for record in records:
        id_station = str(record.get("id", ""))
        if id_station in ids_set:
            out[id_station] = _extract_prix(record, "e85_prix")
    return out


def recuperer_derniere_maj(
    ids_stations: list[str],
    *,
    mock: bool = False,
    chemin_fixture: str | Path | None = None,
    timeout_s: int = 10,
) -> dict[str, str | None]:
    """Renvoie l'horodatage du dernier changement de prix par station.

    L'API expose un timestamp par carburant (`gazole_maj`, `e10_maj`...).
    On garde le PLUS RÉCENT des 4 carburants (= dernière fois où la station
    a touché un de ses prix), formaté `JJ/MM HH:MM`.

    Returns:
        Dict ``{id_station: "27/04 07:30" | None}``.
    """
    if mock:
        records = _charger_fixture(chemin_fixture)
    else:
        records = _appeler_api(ids_stations, timeout_s)

    out: dict[str, str | None] = {}
    ids_set = set(ids_stations)
    for record in records:
        id_station = str(record.get("id", ""))
        if id_station not in ids_set:
            continue
        # Récupérer tous les timestamps maj disponibles
        timestamps = []
        for cle in ("gazole_maj", "e10_maj", "sp98_maj", "e85_maj", "e85_maj"):
            v = record.get(cle)
            if v:
                timestamps.append(str(v))
        if not timestamps:
            out[id_station] = None
            continue
        # Le plus récent (tri lexicographique fonctionne pour ISO 8601)
        plus_recent = max(timestamps)
        # Format API : "2026-04-21T07:30:00+02:00" → "21/04 07:30"
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(plus_recent)
            out[id_station] = dt.strftime("%d/%m %H:%M")
        except (ValueError, TypeError):
            out[id_station] = plus_recent[:16]  # fallback : YYYY-MM-DDTHH:MM
    return out


def _appeler_api(ids_stations: list[str], timeout_s: int) -> list[dict]:
    """Appel HTTP réel. Renvoie la liste `results` ou [] si échec."""
    if not ids_stations:
        return []
    # Filtre ODSQL : id in ("X", "Y", ...)
    clause_ids = " or ".join(f'id="{i}"' for i in ids_stations)
    params = {"where": clause_ids, "limit": LIMIT_DEFAUT}
    try:
        resp = requests.get(API_BASE_URL, params=params, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", [])
    except requests.RequestException as e:
        log.warning("Appel API prix-carburants échoué : %s", e)
        return []
    except json.JSONDecodeError as e:
        log.warning("Réponse API mal formée : %s", e)
        return []


def _charger_fixture(chemin: str | Path | None) -> list[dict]:
    if chemin is None:
        raise ValueError("MOCK_API=true mais chemin_fixture non fourni")
    chemin = Path(chemin)
    if not chemin.is_file():
        raise FileNotFoundError(f"Fixture API introuvable : {chemin}")
    with chemin.open(encoding="utf-8") as f:
        data = json.load(f)
    return data.get("results", [])


def _record_en_prix(record: dict) -> PrixCarburants:
    """Extrait un :class:`PrixCarburants` d'un record API.

    L'API utilise des noms de champs différents pour chaque carburant :
    `gazole_prix`, `e10_prix`, `sp98_prix`, `e85_prix` (pas toujours présents).
    """
    return PrixCarburants(
        gazole=_extract_prix(record, "gazole_prix"),
        sp95_e10=_extract_prix(record, "e10_prix"),
        sp98=_extract_prix(record, "sp98_prix"),
        e85=_extract_prix(record, "e85_prix"),
    )


def _extract_prix(record: dict, cle: str) -> float | None:
    val = record.get(cle)
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
