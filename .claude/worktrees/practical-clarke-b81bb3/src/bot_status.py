"""
bot_status
==========

Helper pour vérifier si le bot est actif au début de chaque workflow.

Usage type au début d'un entry point Python :
    if not bot_actif_ou_skip(racine):
        return 0  # silencieux

Le master flag est dans Google Sheet : Parametres!C20 (OUI/NON).
Modifiable directement par Benjamin via :
- Google Sheet (le plus simple)
- Bouton "PAUSE BOT" dans la question Telegram 11h30
- Workflow toggle_bot.yml (workflow_dispatch)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("bot_status")


def bot_actif_ou_skip(racine: Path) -> bool:
    """Vérifie si le bot est actif. Si non, log et retourne False.

    Le caller doit ``return 0`` (exit success silencieux) si False.
    Si erreur de lecture (pas de creds, pas de connexion), assume actif (fail-safe).
    """
    backend_nom = os.getenv("BACKEND", "gsheet").lower()
    if backend_nom != "gsheet":
        return True  # mode local sans flag, on continue

    try:
        from src.gsheet_io import GSheetIO

        sheet_id = os.getenv("GSHEET_ID", "")
        if not sheet_id or "A_COMPLETER" in sheet_id:
            return True  # pas de Sheet configuré, on continue

        creds_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials/google_sheets_sa.json")
        if not Path(creds_path).is_absolute():
            creds_path = str(racine / creds_path)
        if not Path(creds_path).is_file():
            return True  # pas de creds, on continue

        backend = GSheetIO(sheet_id=sheet_id, credentials_path=creds_path)
        actif = backend.bot_est_actif()
        if not actif:
            log.info("🔕 Bot en pause (Parametres!C20 = NON). Run skippé silencieusement.")
        return actif
    except Exception as e:
        log.warning("Lecture flag bot KO, on continue par défaut : %s", e)
        return True
