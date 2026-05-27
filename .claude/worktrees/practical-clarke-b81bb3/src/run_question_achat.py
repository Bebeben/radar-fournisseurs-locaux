"""Point d'entrée 11h30 — envoie la question achat via Telegram."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.achat_telegram import envoyer_question_achat
from src.moteur_decision import est_jour_actif

log = logging.getLogger("question_achat")


def run() -> int:
    racine = Path(__file__).resolve().parent.parent
    env_file = racine / ".env"
    if env_file.is_file():
        load_dotenv(env_file)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    paris = ZoneInfo("Europe/Paris")
    maintenant = datetime.now(paris)

    # Skip dimanche
    if not est_jour_actif(maintenant.isoweekday(), False):
        log.info("Dimanche — pas de question achat. Bye.")
        return 0

    # Master flag : si bot en pause, exit silencieux
    from src.bot_status import bot_actif_ou_skip
    if not bot_actif_ou_skip(racine):
        return 0

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant")
        return 1

    ok = envoyer_question_achat(bot_token, chat_id)
    log.info("Question achat envoyée : %s", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
