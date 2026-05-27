"""
notifier
========

Notifications push vers Telegram (alternative au mail tant que pas d'email pro).

Configuration via 2 variables d'environnement :
- TELEGRAM_BOT_TOKEN : créé via @BotFather sur Telegram
- TELEGRAM_CHAT_ID  : ton chat ID perso (envoyé par @userinfobot)

Si l'une des 2 vars est vide → la notification est skippée silencieusement.
Pas d'erreur si Telegram down → le run continue normalement.
"""

from __future__ import annotations

import logging
from datetime import datetime

import requests

from src.moteur_decision import CARBURANTS, Action, Proposition

log = logging.getLogger(__name__)


def notifier_telegram(
    proposition: Proposition,
    nos_prix_actuels: dict[str, float | None],
    marge_ponderee: float | None,
    maintenant: datetime,
    bot_token: str,
    chat_id: str,
    sheet_id: str = "",
) -> bool:
    """Envoie un message Telegram avec la décision + prix à mettre.

    Args:
        sheet_id: si fourni, ajoute un lien clickable vers le Google Sheet
            en bas du message + (sur les ACTION) instructions pour accepter
            ou modifier les prix proposés via Telegram.

    Returns:
        True si envoyé, False sinon (token/chat manquant ou erreur HTTP).
    """
    if not bot_token or not chat_id:
        return False

    message = _construire_message(proposition, nos_prix_actuels, marge_ponderee, maintenant, sheet_id)

    # ReplyKeyboard sur les statuts ACTION + INFO URGENT (cascade 4) — décision attendue
    payload: dict = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    necessite_decision = (
        proposition.action == Action.ACTION
        or (proposition.action == Action.INFO and getattr(proposition, "niveau_cascade", None) == 4)
    )
    if necessite_decision:
        payload["reply_markup"] = {
            "keyboard": [
                [{"text": "Appliquer recos"}],
                [{"text": "Modifier prix"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True,
            "input_field_placeholder": "Appliquer recos ou tape: prix vente Gazole=2,200 E10=...",
        }

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.ok:
            log.info("Notification Telegram envoyée")
            return True
        log.warning("Telegram a renvoyé %s : %s", resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as e:
        log.warning("Erreur Telegram (réseau) : %s", e)
        return False


def _construire_message(
    proposition: Proposition,
    nos_prix_actuels: dict[str, float | None],
    marge_ponderee: float | None,
    maintenant: datetime,
    sheet_id: str = "",
) -> str:
    """Format le message Telegram en HTML."""
    horodatage = maintenant.strftime("%d/%m %Hh%M")

    if proposition.action == Action.ACTION:
        # Distinguer cascade niveau 1 (idéal) vs niveau 2 (dégradé)
        if proposition.niveau_cascade == 2:
            emoji = "🚨"
            titre = "ACTION ⚠️ marge pondérée décroche"
        else:
            emoji = "🚨"
            titre = "ACTION"
    elif proposition.action == Action.INFO:
        if proposition.niveau_cascade == 4:
            emoji = "🆘"
            titre = "INFO URGENT (sous plancher)"
        else:
            emoji = "ℹ️"
            titre = "INFO"
    else:
        # STATU QUO : informatif "rien à changer, voici tes marges"
        emoji = "✅"
        titre = "STATU QUO — rien à changer"

    lignes = [
        f"{emoji} <b>{titre}</b> — {horodatage}",
        "",
        f"<i>{proposition.justification}</i>",
        "",
    ]

    if proposition.action == Action.ACTION and proposition.nouveaux_prix:
        lignes.append("<b>Prix à mettre :</b>")
        for c in CARBURANTS:
            nouveau = proposition.nouveaux_prix.get(c)
            ancien = nos_prix_actuels.get(c)
            if nouveau is None:
                continue
            tag = "🆕" if (ancien is not None and abs(nouveau - ancien) >= 0.001) else ""
            lignes.append(f"  • {c} : <b>{_fmt(nouveau)}</b> {tag}".rstrip())
        lignes.append("")
        lignes.append("👉 À déclarer sur prix-carburants.gouv.fr")
        lignes.append("")
        lignes.append("💡 <b>Pour appliquer dans Pricing live :</b>")
        lignes.append("  • Tape <code>accepter</code> = applique tous les prix proposés")
        lignes.append("  • Ou modif partielle : <code>prix vente E85=1,649</code> (1 ou plusieurs carb)")
    elif proposition.action == Action.INFO:
        lignes.append("<b>Tes prix actuels (inchangés) :</b>")
        for c in CARBURANTS:
            p = nos_prix_actuels.get(c)
            if p is not None:
                lignes.append(f"  • {c} : {_fmt(p)}")
        lignes.append("")
        lignes.append("👉 Décision manuelle de ta part")
    else:
        # STATU QUO
        lignes.append("<b>Tes prix actuels (OK) :</b>")
        for c in CARBURANTS:
            p = nos_prix_actuels.get(c)
            if p is not None:
                lignes.append(f"  • {c} : {_fmt(p)}")

    if marge_ponderee is not None:
        lignes.append("")
        lignes.append(f"📊 Marge pondérée : <b>{marge_ponderee * 100:.2f}%</b>".replace(".", ","))

    if sheet_id:
        lignes.append("")
        lignes.append(f'📋 <a href="https://docs.google.com/spreadsheets/d/{sheet_id}/edit">Ouvrir le Sheet</a>')

    return "\n".join(lignes)


def _fmt(prix: float) -> str:
    return f"{prix:.3f} €".replace(".", ",")
