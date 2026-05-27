"""Rappel à 11h25 — si la dernière reco du matin est ACTION et col Suivi vide."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import requests

log = logging.getLogger("rappel_action")
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def run() -> int:
    racine = Path(__file__).resolve().parent.parent
    env_file = racine / ".env"
    if env_file.is_file():
        load_dotenv(env_file)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    paris = ZoneInfo("Europe/Paris")
    maintenant = datetime.now(paris)

    # Master flag : si bot en pause, exit silencieux
    from src.bot_status import bot_actif_ou_skip
    if not bot_actif_ou_skip(racine):
        return 0

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        log.error("Telegram secrets manquants")
        return 1

    # Lire la dernière ligne de Reco prix
    from src.gsheet_io import GSheetIO
    sheet_id = os.getenv("GSHEET_ID", "")
    creds_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials/google_sheets_sa.json")
    if not Path(creds_path).is_absolute():
        creds_path = str(racine / creds_path)
    backend = GSheetIO(sheet_id=sheet_id, credentials_path=creds_path)

    # Lire ligne 2 de Reco prix (la plus récente, newest-on-top)
    # Cols : A=Date, B=Heure, C=Statut, ..., L=Marge ponderee, M=Niveau cascade, N=Raison, O=Suivi
    rows = backend._read_range("Reco prix!A2:O2")
    if not rows or not rows[0]:
        log.info("Pas de ligne Reco prix — rien à rappeler.")
        return 0

    row = rows[0]
    date_run = row[0] if len(row) > 0 else ""
    heure_run = row[1] if len(row) > 1 else ""
    statut = row[2] if len(row) > 2 else ""
    raison = row[13] if len(row) > 13 else ""
    suivi = row[14] if len(row) > 14 else ""

    # On rappelle UNIQUEMENT si :
    # - dernière ligne est de TODAY
    # - statut = ACTION
    # - col Suivi vide (pas encore traité)
    today_iso = maintenant.date().isoformat()
    if date_run != today_iso:
        log.info("Dernière reco pas de ce matin (date=%s vs today=%s) — pas de rappel", date_run, today_iso)
        return 0
    if statut != "ACTION":
        log.info("Dernière reco pas ACTION (statut=%s) — pas de rappel", statut)
        return 0
    if suivi and str(suivi).strip().upper() in ("OUI", "NON", "PARTIEL", "N/A"):
        log.info("Reco déjà traitée (Suivi=%s) — pas de rappel", suivi)
        return 0

    # Envoyer rappel
    message = (
        f"🔔 <b>Rappel ACTION pending</b>\n\n"
        f"Reco de ce matin {heure_run} :\n"
        f"<i>{raison}</i>\n\n"
        f"As-tu appliqué les prix recommandés en caisse + déclaré sur prix-carburants.gouv.fr ?\n\n"
        f"Pense à mettre <b>OUI</b> ou <b>NON</b> dans la col Suivi de Reco prix (Sheet) "
        f"pour traçabilité."
    )
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=bot_token),
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.ok:
            log.info("Rappel ACTION envoyé")
            return 0
        log.warning("Telegram a renvoyé %s", resp.status_code)
        return 1
    except requests.RequestException as e:
        log.warning("Erreur Telegram : %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(run())
