"""
diagnostic_telegram
===================

Script one-shot : interroge Telegram + Sheet et envoie un rapport
diagnostic via Telegram pour identifier pourquoi le polling rate
les messages.

Lance via le workflow `.github/workflows/telegram_diagnostic.yml`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests


def main() -> int:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    sheet_id = os.getenv("GSHEET_ID", "")
    if not bot_token or not chat_id:
        print("Secrets Telegram manquants")
        return 1

    rapport = ["🔬 <b>DIAGNOSTIC POLLING TELEGRAM</b>", ""]

    # 1. Sheet : last_update_id stocké
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials/google_sheets_sa.json")
        if not Path(creds_path).is_absolute():
            creds_path = str(Path(__file__).resolve().parent.parent / creds_path)
        creds = service_account.Credentials.from_service_account_file(
            creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        res = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range="Parametres!C25"
        ).execute()
        stored = res.get("values", [["0"]])[0][0] if res.get("values") else "0"
        rapport.append(f"📋 Sheet last_update_id : <code>{stored}</code>")
    except Exception as e:
        rapport.append(f"📋 Sheet last_update_id : ERREUR {e}")
        stored = "0"

    # 2. getWebhookInfo
    try:
        r = requests.get(f"https://api.telegram.org/bot{bot_token}/getWebhookInfo", timeout=10)
        info = r.json().get("result", {})
        webhook_url = info.get("url", "")
        if webhook_url:
            rapport.append(f"⚠️ <b>Webhook ACTIF</b> : {webhook_url}")
            rapport.append(f"   pending_update_count : {info.get('pending_update_count', '?')}")
            rapport.append(f"   <i>BUG : getUpdates est inutilisable tant qu'un webhook est actif.</i>")
        else:
            rapport.append("✅ Aucun webhook actif (getUpdates OK)")
    except Exception as e:
        rapport.append(f"❌ getWebhookInfo KO : {e}")

    # 3. getUpdates SANS offset (état complet du buffer)
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getUpdates",
            params={"timeout": 1, "limit": 100},
            timeout=10,
        )
        data = r.json()
        if not data.get("ok"):
            rapport.append(f"❌ getUpdates erreur : {data.get('description', '?')}")
        else:
            updates = data.get("result", [])
            rapport.append(f"📨 getUpdates SANS offset : <b>{len(updates)}</b> message(s) en buffer")
            if updates:
                first_id = updates[0].get("update_id")
                last_id = updates[-1].get("update_id")
                rapport.append(f"   update_id : {first_id} → {last_id}")
                rapport.append("")
                rapport.append("<b>Derniers messages (max 5) :</b>")
                for u in updates[-5:]:
                    uid = u.get("update_id")
                    msg = u.get("message", {})
                    txt = msg.get("text", "")[:50]
                    rapport.append(f"  [{uid}] {txt}")
    except Exception as e:
        rapport.append(f"❌ getUpdates KO : {e}")

    # 4. getUpdates AVEC offset = last_update_id + 1
    try:
        offset = int(stored) + 1 if stored.isdigit() else 1
        r = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getUpdates",
            params={"timeout": 1, "limit": 100, "offset": offset},
            timeout=10,
        )
        data = r.json()
        updates = data.get("result", [])
        rapport.append("")
        rapport.append(f"📨 getUpdates AVEC offset={offset} : <b>{len(updates)}</b> message(s)")
        if updates:
            rapport.append("   (le polling devrait les traiter au prochain run)")
        else:
            rapport.append("   <i>Aucun message → soit Telegram a déjà ack ces id, soit aucun nouveau message</i>")
    except Exception as e:
        rapport.append(f"❌ getUpdates avec offset KO : {e}")

    # Envoi rapport
    message = "\n".join(rapport)
    print(message)  # log GH Actions
    requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
        timeout=10,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
