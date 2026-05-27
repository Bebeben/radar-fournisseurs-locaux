"""Point d'entrée 12h30 — batch lecture des messages Telegram du jour.

Flow :
1. Lit TOUS les messages Telegram reçus depuis 7h ce matin
2. Pour chaque message format "prix ..." → insère ligne dans Prix d'achat
   - Détermine "J" ou "J+1" selon heure bascule (Parametres L19)
   - Anti-doublon : ne pas réinsérer si même prix déjà présent
   - Détection variation anormale : alerte Telegram si écart > 10 cts/L vs dernier
3. Détecte le dernier message "oui"/"non" envoyé après 9h00
   → réponse à la question achat 9h30
4. Si OUI : prend le dernier prix balisé "J" du jour et met à jour Pricing live + ajoute Commande
5. Si OUI mais aucun prix "J" reçu : envoie message Telegram demandant les prix
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.achat_telegram import (
    calculer_date_livraison,
    determiner_pour_jour,
    detecter_variation_anormale,
    envoyer_alerte_sanity,
    envoyer_confirmation_achat,
    lire_messages_telegram,
    parser_message_prix,
    parser_reponse_achat,
    sanity_check_prix,
)
from src.gsheet_io import GSheetIO

log = logging.getLogger("lecture_reponse")

import requests
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _envoyer_message(bot_token: str, chat_id: str, texte: str) -> None:
    """Helper : envoi simple d'un message Telegram."""
    if not bot_token or not chat_id:
        return
    try:
        requests.post(
            TELEGRAM_API.format(token=bot_token),
            json={"chat_id": chat_id, "text": texte, "parse_mode": "HTML"},
            timeout=10,
        )
    except requests.RequestException:
        pass


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

    # Master flag : si bot en pause, exit silencieux
    from src.bot_status import bot_actif_ou_skip
    if not bot_actif_ou_skip(racine):
        return 0

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant")
        return 1

    # Backend Sheet
    sheet_id = os.getenv("GSHEET_ID", "")
    creds_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials/google_sheets_sa.json")
    if not Path(creds_path).is_absolute():
        creds_path = str(racine / creds_path)
    backend = GSheetIO(sheet_id=sheet_id, credentials_path=creds_path)

    # Heure bascule + params livraison
    heure_bascule = backend.lire_heure_bascule()
    log.info("Heure bascule J/J+1 : %s", heure_bascule)

    # Fenêtre de lecture : 7h00 jusqu'à maintenant
    debut_fenetre = maintenant.replace(hour=7, minute=0, second=0, microsecond=0)
    messages = lire_messages_telegram(bot_token, chat_id, debut_fenetre)
    log.info("Messages reçus depuis 7h00 : %d", len(messages))

    # 1. Process messages "prix ..." → insère dans Prix d'achat
    nb_prix_inseres = 0
    nb_doublons = 0
    for msg in sorted(messages, key=lambda m: m["date"]):
        prix = parser_message_prix(msg["text"])
        if prix is None:
            continue
        msg_dt = msg["date"].astimezone(paris) if msg["date"].tzinfo else msg["date"].replace(tzinfo=paris)
        pour_jour = determiner_pour_jour(msg_dt, heure_bascule)

        # Détection variation anormale vs dernier prix d'achat
        precedent = backend.lire_dernier_prix_achat()
        alertes = detecter_variation_anormale(prix, precedent, seuil_eur=0.10)
        if alertes:
            log.warning("Variation anormale : %s", alertes)
            _envoyer_message(
                bot_token, chat_id,
                "⚠️ <b>Variation anormale détectée</b>\n\n" + "\n".join(f"  • {a}" for a in alertes)
                + "\n\n<i>Prix quand même enregistré dans Prix d'achat.</i>"
            )

        # Insertion (anti-doublon dans la fonction)
        ajoute = backend.append_prix_achat(
            date_jour=msg_dt.date(),
            heure_message=msg_dt,
            pour_jour=pour_jour,
            prix=prix,
            source="Telegram texte",
            variation_anormale=alertes,
        )
        if ajoute:
            nb_prix_inseres += 1
            log.info("Prix inseré : pour=%s %s", pour_jour, prix)
        else:
            nb_doublons += 1
            log.info("Doublon ignoré : %s", prix)

    log.info("Prix d'achat : %d inseré(s), %d doublon(s) ignoré(s)", nb_prix_inseres, nb_doublons)

    # 2. Détection réponse oui/non à la question 9h30
    # Fenêtre depuis 9h00 pour capter une réponse au plus tôt (avec marge avant 9h30).
    debut_fenetre_reponse = maintenant.replace(hour=9, minute=0, second=0, microsecond=0)
    # m["date"] est aware UTC → on convertit en Paris pour comparer avec debut_fenetre_reponse (paris)
    reponses_apres_11h = [
        m for m in messages
        if (m["date"].astimezone(paris) if m["date"].tzinfo else m["date"].replace(tzinfo=paris)) >= debut_fenetre_reponse
    ]

    # Garder uniquement les messages oui/non (pas les "prix ...")
    reponses_oui_non = []
    for m in reponses_apres_11h:
        parsed = parser_reponse_achat(m["text"])
        if parsed is not None:
            reponses_oui_non.append((m, parsed))

    if not reponses_oui_non:
        log.info("Aucune réponse oui/non — pas d'achat ce jour (silence = NON)")
        return 0

    # Prendre la dernière réponse
    dernier_msg, dernier_parsed = max(reponses_oui_non, key=lambda x: x[0]["date"])
    log.info("Dernière réponse : %s -> %s", dernier_msg["text"][:50], dernier_parsed)

    # Cas pause bot : toggle le flag à NON
    if dernier_parsed.get("pause_bot"):
        log.info("Réponse 'pause bot' — désactivation du master flag")
        backend.set_bot_actif(False)
        _envoyer_message(
            bot_token, chat_id,
            "🔕 <b>Bot mis en pause</b>\n\n"
            "Tous les workflows sont désactivés (Pricing, Question achat, Récap, Rappel).\n\n"
            "Pour reprendre :\n"
            "• Direct dans Google Sheet : <i>Parametres ligne 20 → OUI</i>\n"
            "• Ou workflow GitHub <code>Toggle bot</code> avec action=Reprise"
        )
        return 0

    if not dernier_parsed.get("achat"):
        log.info("Réponse 'non' — pas d'achat aujourd'hui")
        return 0

    # 3. OUI : on prend les derniers prix balisés "J" du jour pour maj Pricing live
    prix_du_jour = backend.lire_prix_achat_du_jour(maintenant.date(), pour_jour="J")
    if not prix_du_jour:
        log.warning("Réponse OUI mais aucun prix 'J' reçu aujourd'hui")
        _envoyer_message(
            bot_token, chat_id,
            "🛒 <b>Tu as commandé, mais je n'ai pas tes prix du jour.</b>\n\n"
            "Tape les prix d'achat HT au format :\n"
            "<code>prix E85=1,234 E10=1,234 SP98=1,234 GAZ=1,234</code>\n\n"
            "<i>Je relancerai à 13h pour vérifier.</i>"
        )
        return 0

    # Prendre le PLUS RÉCENT (lire_prix_achat_du_jour renvoie dans l'ordre de lecture du Sheet, newest-on-top)
    dernier = prix_du_jour[0]
    prix_a_appliquer = {
        "E85": dernier.get("E85"),
        "SP95-E10": dernier.get("SP95-E10"),
        "SP98": dernier.get("SP98"),
        "Gazole": dernier.get("Gazole"),
    }
    prix_a_appliquer = {k: v for k, v in prix_a_appliquer.items() if v is not None}

    # Sanity check (sauf si "force" dans la réponse oui)
    if not dernier_parsed.get("force"):
        precedent_pricing_live, _ = backend.lire_pricing_live()
        ok, alertes = sanity_check_prix(prix_a_appliquer, precedent_pricing_live.as_dict())
        if not ok:
            log.warning("Sanity check KO : %s", alertes)
            envoyer_alerte_sanity(bot_token, chat_id, alertes, prix_a_appliquer)
            return 0

    # 4. Maj Pricing live
    backend.maj_pricing_live_achat(prix_a_appliquer)
    log.info("Pricing live mis à jour : %s", prix_a_appliquer)

    # 5. Calcul date livraison + ajout Commande
    params_liv = backend.lire_parametres_livraison()
    exception_samedi = dernier_parsed.get("livraison_samedi", False)
    date_livraison = calculer_date_livraison(
        date_commande=maintenant.date(),
        delai_jours=params_liv["delai_jours"],
        livraison_samedi_possible=params_liv["samedi_possible"],
        exception_samedi=exception_samedi,
    )
    backend.append_commande(
        date_commande=maintenant.date(),
        date_livraison=date_livraison,
        prix_achat_ht=prix_a_appliquer,
        supplement_samedi=exception_samedi,
    )
    log.info("Commande enregistrée. Livraison prévue : %s", date_livraison)

    # 6. Confirmation Telegram
    envoyer_confirmation_achat(bot_token, chat_id, prix_a_appliquer, date_livraison, exception_samedi)
    log.info("Run terminé.")
    return 0


def _notifier_crash_telegram(exception: Exception, contexte: str) -> None:
    """Alerte Telegram si crash. Évite les crashs silencieux sur GitHub Actions."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    try:
        import traceback
        tb_short = "\n".join(traceback.format_exc().splitlines()[-8:])
        message = (
            f"🆘 <b>Crash workflow {contexte}</b>\n\n"
            f"<code>{type(exception).__name__}: {exception}</code>\n\n"
            f"<i>Trace (extrait) :</i>\n<pre>{tb_short[:1500]}</pre>"
        )
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


if __name__ == "__main__":
    try:
        code = run()
    except Exception as e:
        log.error("CRASH run_lecture_reponse : %s", e, exc_info=True)
        _notifier_crash_telegram(e, contexte="lecture reponse 12h")
        sys.exit(1)
    sys.exit(code)
