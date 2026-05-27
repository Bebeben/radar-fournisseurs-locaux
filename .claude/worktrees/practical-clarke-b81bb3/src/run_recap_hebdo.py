"""
run_recap_hebdo
===============

Point d'entrée du workflow récap hebdomadaire (lundi 7h).

Lit l'historique du Sheet (Reco prix + Concurrents), génère le mail récap,
l'écrit dans un nouvel onglet `Recap hebdo` du Sheet (et plus tard l'envoie
par mail quand le connecteur Gmail sera actif).

Usage : `python -m src.run_recap_hebdo`
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.recap_hebdo import generer_recap_hebdo

log = logging.getLogger("recap")


def run() -> int:
    racine = Path(__file__).resolve().parent.parent
    env_file = racine / ".env"
    if env_file.is_file():
        load_dotenv(env_file)
    else:
        load_dotenv(racine / ".env.example")

    log_level = os.getenv("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    backend_nom = os.getenv("BACKEND", "gsheet").lower()
    if backend_nom != "gsheet":
        log.error("Récap hebdo nécessite BACKEND=gsheet (lecture historique). Backend actuel : %s", backend_nom)
        return 1

    # Master flag : si bot en pause, exit silencieux
    from src.bot_status import bot_actif_ou_skip
    if not bot_actif_ou_skip(racine):
        return 0

    from src.gsheet_io import GSheetIO

    sheet_id = os.getenv("GSHEET_ID", "")
    creds_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials/google_sheets_sa.json")
    if not Path(creds_path).is_absolute():
        creds_path = str(racine / creds_path)

    backend = GSheetIO(sheet_id=sheet_id, credentials_path=creds_path)

    paris = ZoneInfo("Europe/Paris")
    aujourd_hui = datetime.now(paris).date()

    # Lire l'historique des 14 derniers jours
    log.info("Lecture historique Reco prix + Concurrents...")
    reco_prix = _lire_reco_prix_complet(backend)
    concurrents = _lire_concurrents_complet(backend)
    log.info("Reco prix : %d lignes | Concurrents : %d lignes", len(reco_prix), len(concurrents))

    # Générer le récap
    corps = generer_recap_hebdo(reco_prix, concurrents, aujourd_hui)
    if not corps:
        log.warning("Pas assez de données pour générer un récap (faut au moins 1 semaine d'historique)")
        return 0

    log.info("Récap généré (%d caractères)", len(corps))
    print("\n" + "=" * 60)
    print(corps)
    print("=" * 60 + "\n")

    # Écrire dans un onglet "Recap hebdo" pour archive (créé automatiquement si inexistant)
    _ecrire_recap_dans_sheet(backend, aujourd_hui, corps)

    # TODO post-cession : envoi mail si MAIL_ACTIF=true et connecteur Gmail OK
    if os.getenv("MAIL_ACTIF", "false").lower() == "true":
        log.info("[TODO] Envoi mail récap via Gmail API — à finaliser lors du déploiement")

    log.info("Run récap terminé.")
    return 0


def _lire_reco_prix_complet(backend) -> list[dict]:
    """Lit toutes les lignes de Reco prix (col A à L)."""
    rows = backend._read_range("Reco prix!A2:L500")
    headers = ["Date", "Heure", "Statut", "E85 TTC", "SP95-E10 TTC", "SP98 TTC", "Gazole TTC",
               "Marge SP95 %", "Marge SP95-E10 %", "Marge SP98 %", "Marge Gazole %", "Marge pondérée %"]
    out = []
    for row in rows:
        d = {}
        for i, h in enumerate(headers):
            d[h] = row[i] if i < len(row) else ""
        out.append(d)
    return out


def _lire_concurrents_complet(backend) -> list[dict]:
    """Lit toutes les lignes de Concurrents (col A à L)."""
    rows = backend._read_range("Concurrents!A2:L1000")
    headers = ["Date", "Heure", "Station", "Type", "Distance km", "Dernière maj concurrent",
               "E85", "SP95-E10", "SP98", "Gazole", "E85", "Notes"]
    out = []
    for row in rows:
        d = {}
        for i, h in enumerate(headers):
            d[h] = row[i] if i < len(row) else ""
        out.append(d)
    return out


def _ecrire_recap_dans_sheet(backend, aujourd_hui: date, corps: str) -> None:
    """Écrit le récap dans un onglet `Recap hebdo` (1 ligne par lundi)."""
    try:
        # Vérifier que l'onglet existe, sinon le créer
        meta = backend._service.spreadsheets().get(spreadsheetId=backend.sheet_id).execute()
        existing = [s["properties"]["title"] for s in meta["sheets"]]
        if "Recap hebdo" not in existing:
            req = {
                "addSheet": {
                    "properties": {
                        "title": "Recap hebdo",
                        "gridProperties": {"frozenRowCount": 1},
                    }
                }
            }
            backend._service.spreadsheets().batchUpdate(
                spreadsheetId=backend.sheet_id, body={"requests": [req]}
            ).execute()
            # Headers
            backend._append_range("Recap hebdo!A:B", [["Date du lundi", "Récap"]])

        # Append le récap
        backend._append_range("Recap hebdo!A:B", [[aujourd_hui.isoformat(), corps]])
        log.info("Récap archivé dans onglet 'Recap hebdo'")
    except Exception as e:
        log.warning("Impossible d'archiver le récap dans le Sheet : %s", e)


if __name__ == "__main__":
    sys.exit(run())
