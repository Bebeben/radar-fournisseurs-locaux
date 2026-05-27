"""
achat_telegram
==============

Workflow d'achat carburant via Telegram.

11h30 — questionne Benjamin "T'as commandé aujourd'hui ?"
12h00 — lit la réponse, parse les prix d'achat HT, met à jour Pricing live + onglet Commande

Format de réponse attendu :
    "non"
    OU
    "oui E85=1,234 E10=1,234 SP98=1,234 GAZ=1,234"
    OU (cas exceptionnel livraison samedi)
    "oui samedi E85=... E10=... SP98=... GAZ=..."
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"


# ============================================================
# Envoi question
# ============================================================


def envoyer_question_achat(bot_token: str, chat_id: str) -> bool:
    """Envoie le message Telegram "T'as commandé ?" avec boutons OUI/NON.

    Si OUI : le système prendra automatiquement les derniers prix balisés "J"
    reçus aujourd'hui (via messages "prix ..." postés dans la journée) pour maj
    Pricing live. Si aucun prix "J" reçu, il te demandera de les saisir.

    Format pour saisie manuelle des prix (à n'importe quel moment de la journée) :
        prix E85=1,234 E10=1,234 SP98=1,234 GAZ=1,234
    """
    if not bot_token or not chat_id:
        return False
    message = (
        "🛒 <b>Commande carburant aujourd'hui ?</b>\n\n"
        "Clique <b>OUI</b> ou <b>NON</b> ⬇️\n\n"
        "<b>Si OUI</b> : je prendrai les derniers prix \"J\" déjà saisis pour maj Pricing live.\n"
        "Si tu n'as pas encore saisi les prix, tape :\n"
        "<code>prix E85=1,234 E10=1,234 SP98=1,234 GAZ=1,234</code>\n\n"
        "<b>Cas exceptionnel</b> : livraison samedi (vendredi seulement) :\n"
        "<code>oui samedi</code>\n\n"
        "<i>Pas de réponse = pas de commande aujourd'hui.</i>"
    )
    # ReplyKeyboard avec 3 boutons OUI / NON / PAUSE BOT
    keyboard = {
        "keyboard": [
            [{"text": "oui"}, {"text": "non"}],
            [{"text": "pause bot"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
        "input_field_placeholder": "OUI / NON ou pause bot",
    }
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=bot_token) + "/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "reply_markup": keyboard,
            },
            timeout=10,
        )
        return resp.ok
    except requests.RequestException as e:
        log.warning("Telegram envoi question achat KO : %s", e)
        return False


# ============================================================
# Lecture réponse
# ============================================================


def lire_messages_telegram(
    bot_token: str,
    chat_id: str,
    apres_timestamp: datetime | None = None,
    offset: int | None = None,
) -> list[dict]:
    """Récupère les messages reçus du chat.

    Deux modes :
    - **offset** (recommandé) : passe ``offset = last_update_id + 1``. Telegram
      ne renvoie que les updates avec id ≥ offset. Aucun message raté, même si
      le polling tourne peu fréquemment. Le caller doit ensuite stocker le
      ``update_id`` max retourné pour le run suivant.
    - **apres_timestamp** (legacy) : filtre par timestamp. Risque de rater
      des messages si polling > 24h.

    Returns:
        Liste de dicts ``{date, text, update_id}`` pour chaque message du chat.
        ``date`` est aware UTC. ``update_id`` permet au caller de mettre à jour
        son curseur de lecture.
    """
    if not bot_token or not chat_id:
        return []
    params: dict = {"timeout": 5, "limit": 100}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(
            TELEGRAM_API.format(token=bot_token) + "/getUpdates",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("Telegram getUpdates KO : %s", e)
        return []

    # Si on a un apres_timestamp (mode legacy), normaliser en aware UTC.
    from zoneinfo import ZoneInfo
    apres_utc = None
    if apres_timestamp is not None:
        if apres_timestamp.tzinfo is None:
            apres_timestamp = apres_timestamp.replace(tzinfo=ZoneInfo("Europe/Paris"))
        apres_utc = apres_timestamp.astimezone(ZoneInfo("UTC"))

    messages = []
    for update in data.get("result", []):
        msg = update.get("message", {})
        if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
            continue
        ts = msg.get("date", 0)  # epoch UTC (Telegram envoie toujours en UTC)
        msg_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("UTC"))  # AWARE UTC
        if apres_utc is not None and msg_dt < apres_utc:
            continue
        text = msg.get("text", "").strip()
        if text:
            messages.append({
                "date": msg_dt,
                "text": text,
                "update_id": update.get("update_id", 0),
            })
    return messages


# ============================================================
# Parsing réponse
# ============================================================


# Bornes de plausibilité pour un prix d'achat carburant HT en €/L (France 2026)
PRIX_MIN_PLAUSIBLE = 0.80
PRIX_MAX_PLAUSIBLE = 2.50
# Variation maximale acceptée vs prix d'achat précédent (= alerte si écart > seuil)
VARIATION_SUSPECTE_EUR = 0.30

REGEX_PRIX = {
    "E85": re.compile(r"E85\s*=\s*(\d+[,.]?\d*)", re.IGNORECASE),
    "SP95-E10": re.compile(r"(?:SP95[-_ ]?)?E10\s*=\s*(\d+[,.]?\d*)", re.IGNORECASE),
    "SP98": re.compile(r"SP98\s*=\s*(\d+[,.]?\d*)", re.IGNORECASE),
    # Accepte : GAZ=, GAZOLE=, GASOIL=, DIESEL=  (insensible casse)
    "Gazole": re.compile(r"(?:GAZOLE|GASOIL|DIESEL|GAZ)\s*=\s*(\d+[,.]?\d*)", re.IGNORECASE),
}


def sanity_check_prix(
    prix_saisis: dict[str, float],
    prix_precedents: dict[str, float | None] | None = None,
) -> tuple[bool, list[str]]:
    """Vérifie la plausibilité des prix saisis.

    Returns:
        (ok, alertes). Si ok=False, alertes liste les anomalies détectées.
    """
    alertes: list[str] = []
    for carb, prix in prix_saisis.items():
        # Borne plausible
        if prix < PRIX_MIN_PLAUSIBLE or prix > PRIX_MAX_PLAUSIBLE:
            alertes.append(
                f"{carb} = {prix:.3f} € hors plage plausible "
                f"[{PRIX_MIN_PLAUSIBLE}-{PRIX_MAX_PLAUSIBLE} €/L HT]"
            )
            continue
        # Variation vs précédent
        if prix_precedents:
            ancien = prix_precedents.get(carb)
            if ancien is not None and ancien > 0:
                ecart = abs(prix - ancien)
                if ecart > VARIATION_SUSPECTE_EUR:
                    alertes.append(
                        f"{carb} : {prix:.3f} € vs précédent {ancien:.3f} € "
                        f"(écart {ecart * 100:.1f} cts > {VARIATION_SUSPECTE_EUR * 100:.0f} cts seuil)"
                    )
    return (len(alertes) == 0), alertes


def envoyer_alerte_sanity(
    bot_token: str, chat_id: str, alertes: list[str], prix_saisis: dict
) -> bool:
    """Envoie un message Telegram d'alerte sur prix suspects."""
    if not bot_token or not chat_id:
        return False
    lignes = ["⚠️ <b>Prix saisis suspects</b>", ""]
    for a in alertes:
        lignes.append(f"  • {a}")
    lignes.append("")
    lignes.append("Vérifie tes prix puis re-saisis si besoin avec la même commande :")
    parts = " ".join(f"{c}={p:.3f}".replace(".", ",") for c, p in prix_saisis.items())
    lignes.append(f"<code>oui {parts}</code>")
    lignes.append("")
    lignes.append("<i>Pour forcer (saisie OK) : préfixe 'oui force' au lieu de 'oui'.</i>")
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=bot_token) + "/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lignes), "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.ok
    except requests.RequestException:
        return False


def parser_message_prix(texte: str) -> dict[str, float] | None:
    """Détecte un message qui contient des prix d'achat HT et les extrait.

    Préfixe "prix" optionnel — tout message contenant `gaz=X,XX` (ou autre
    carburant reconnu) est interprété comme prix d'achat.

    Formats acceptés :
        prix E85=1,234 E10=1,234 SP98=1,234 GAZ=1,234
        gaz=2,1
        gazole=2,06 e10=1,58

    Exclu :
        - "oui ..." / "non" (gérés par parser_reponse_achat)
        - "prix vente ..." / "vente ..." (gérés par parser_message_prix_vente)

    Returns:
        Dict ``{carburant: prix}`` ou None si aucun carburant reconnu.
    """
    t = texte.lower().strip()
    # Exclure les commandes gérées par d'autres parsers
    if t.startswith("oui") or t.startswith("non"):
        return None
    if t.startswith("prix vente") or t.startswith("vente "):
        return None

    prix = {}
    for carb, regex in REGEX_PRIX.items():
        m = regex.search(texte)
        if m:
            val = m.group(1).replace(",", ".")
            try:
                prix[carb] = float(val)
            except ValueError:
                continue

    return prix if prix else None


def parser_message_prix_vente(texte: str) -> dict[str, float] | None:
    """Détecte un message "prix vente E85=... E10=... SP98=... GAZ=..." (modif partielle OK).

    Concerne les prix de VENTE TTC (différent de parser_message_prix qui est pour l'ACHAT).
    Permet à Benjamin d'overrider partiellement les prix proposés par le bot après
    un run ACTION, ex : "prix vente E85=1,649" → on remplace seulement le SP95.

    Returns:
        Dict ``{carburant: prix}`` (potentiellement partiel) ou None si format pas reconnu.
    """
    t = texte.lower().strip()
    if not (t.startswith("prix vente") or t.startswith("vente")):
        return None

    prix = {}
    for carb, regex in REGEX_PRIX.items():
        m = regex.search(texte)
        if m:
            val = m.group(1).replace(",", ".")
            try:
                prix[carb] = float(val)
            except ValueError:
                continue

    return prix if prix else None


def est_commande_accept(texte: str) -> bool:
    """Détecte les messages d'acceptation des prix de vente proposés.

    Reconnu (liste exhaustive) :
    - accept / accepter / j'accepte / acceptation
    - ok / ok prix / ok prix vente / ok aligne / ok alignement / ok appliquer
    - applique / appliquer / appliquer recos / appliquer reco
    - valide / valider / valider recos / valider reco / validation
    - aligne / aligner / alignement / ok aligne
    - go
    """
    t = texte.lower().strip()
    return t in (
        # Famille "accept"
        "accept", "accepter", "j'accepte", "acceptation",
        # Famille "ok"
        "ok", "ok prix", "ok prix vente",
        "ok aligne", "ok alignement", "ok appliquer", "ok valider", "ok recos",
        # Famille "appliquer"
        "appliquer", "applique",
        "appliquer recos", "appliquer reco", "applique recos", "applique reco",
        # Famille "valider"
        "valider", "valide", "validation",
        "valider recos", "valider reco", "valide recos", "valide reco",
        # Famille "aligner" (= ce que Benjamin a tapé le 13/05)
        "aligne", "aligner", "alignement", "alignment",
        # Misc
        "go", "vas-y", "vas y",
    )


def est_commande_modifier(texte: str) -> bool:
    """Détecte "Modifier prix" : ouvre une suggestion de saisie custom.

    Le bouton "Modifier prix" du ReplyKeyboard envoie ce texte ; le polling
    répond avec un message d'aide expliquant comment taper la commande prix.
    """
    t = texte.lower().strip()
    return t in ("modifier", "modifier prix", "modifier prix vente", "modif prix")


def est_commande_aide(texte: str) -> bool:
    """Détecte les demandes d'aide / liste des commandes disponibles.

    Reconnu : "aide", "help", "/help", "commandes", "commande", "menu", "?".
    """
    t = texte.lower().strip()
    # Cas spéciaux : ? ou ?? tout seul = aide
    if t in ("?", "??", "???"):
        return True
    # Si finit par ?, on enlève pour permettre "aide ?"
    t = t.rstrip("?").strip()
    return t in (
        "aide", "help", "/help", "/aide",
        "commande", "commandes", "liste",
        "menu", "que faire", "quoi taper",
    )


def lien_sheet(sheet_id: str) -> str:
    """Renvoie l'URL clickable du Google Sheet pour les messages Telegram."""
    if not sheet_id:
        return ""
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"


def envoyer_photo_telegram(
    bot_token: str,
    chat_id: str,
    png_bytes: bytes,
    caption: str = "",
) -> bool:
    """Envoie une image PNG via Telegram sendPhoto.

    Args:
        png_bytes : contenu binaire du PNG (depuis matplotlib).
        caption : légende HTML optionnelle.

    Returns:
        True si envoyé OK, False sinon.
    """
    if not bot_token or not chat_id or not png_bytes:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    try:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("releve_concurrence.png", png_bytes, "image/png")},
            timeout=30,
        )
        return resp.ok
    except requests.RequestException as e:
        log.warning("Telegram sendPhoto KO : %s", e)
        return False


def determiner_pour_jour(heure_message: datetime, heure_bascule: str = "11:00") -> str:
    """Détermine si le prix communiqué concerne aujourd'hui (J) ou demain (J+1).

    Args:
        heure_message: timestamp du message Telegram (Europe/Paris).
        heure_bascule: format HH:MM, défaut 11:00. Avant = J, après = J+1.

    Returns:
        "J" ou "J+1"
    """
    try:
        h, m = heure_bascule.split(":")
        bascule_h, bascule_m = int(h), int(m)
    except (ValueError, AttributeError):
        bascule_h, bascule_m = 11, 0

    bascule_minutes = bascule_h * 60 + bascule_m
    msg_minutes = heure_message.hour * 60 + heure_message.minute
    return "J" if msg_minutes < bascule_minutes else "J+1"


def detecter_variation_anormale(
    prix_nouveaux: dict[str, float],
    prix_precedents: dict[str, float] | None,
    seuil_eur: float = 0.10,
) -> list[str]:
    """Renvoie une liste d'alertes si certains prix varient > seuil_eur vs les précédents."""
    alertes = []
    if not prix_precedents:
        return alertes
    for carb, nouveau in prix_nouveaux.items():
        ancien = prix_precedents.get(carb)
        if ancien is None or ancien <= 0:
            continue
        ecart = abs(nouveau - ancien)
        if ecart > seuil_eur:
            alertes.append(
                f"{carb} : {nouveau:.3f} € vs précédent {ancien:.3f} € "
                f"(écart {ecart * 100:.1f} cts > {seuil_eur * 100:.0f} cts seuil)"
            )
    return alertes


def parser_reponse_achat(texte: str) -> dict[str, Any] | None:
    """Parse la réponse Benjamin et renvoie un dict structuré.

    Returns:
        - None si indéchiffrable
        - dict avec clés :
            achat (bool), prix (dict), livraison_samedi (bool), force (bool)
            pause_bot (bool) : si True, le caller doit toggler le flag à NON
    """
    t = texte.lower().strip()

    # Cas pause bot
    if "pause bot" in t or t in ("pause", "/pause"):
        return {"achat": False, "pause_bot": True}

    # Cas non
    if t == "non" or t.startswith("non "):
        return {"achat": False}

    # Cas oui
    if not t.startswith("oui"):
        return None

    livraison_samedi = "samedi" in t
    force = "force" in t  # bypass sanity check si Benjamin a confirmé

    prix = {}
    for carb, regex in REGEX_PRIX.items():
        m = regex.search(texte)
        if m:
            val = m.group(1).replace(",", ".")
            try:
                prix[carb] = float(val)
            except ValueError:
                continue

    # "oui" seul (depuis bouton OUI) est valide → les prix viennent séparément (messages "prix ...")
    return {
        "achat": True,
        "prix": prix,  # peut être vide si bouton OUI cliqué sans saisie
        "livraison_samedi": livraison_samedi,
        "force": force,
    }


# ============================================================
# Calcul date livraison
# ============================================================


def calculer_date_livraison(
    date_commande: date,
    delai_jours: int = 1,
    livraison_samedi_possible: bool = False,
    exception_samedi: bool = False,
) -> date:
    """Calcule la date de livraison en respectant les contraintes.

    Règles :
    - Par défaut : J + delai_jours
    - Si la livraison tombe un dimanche : passer au lundi
    - Si la livraison tombe un samedi ET livraison_samedi_possible=False ET
      pas exception_samedi : passer au lundi
    """
    livraison = date_commande + timedelta(days=delai_jours)
    while True:
        wd = livraison.isoweekday()  # lundi=1, dimanche=7
        if wd == 7:  # dimanche jamais
            livraison += timedelta(days=1)
            continue
        if wd == 6:  # samedi
            if livraison_samedi_possible or exception_samedi:
                break  # samedi OK
            livraison += timedelta(days=2)  # passer à lundi
            continue
        break
    return livraison


# ============================================================
# Confirmation
# ============================================================


def envoyer_confirmation_achat(
    bot_token: str, chat_id: str,
    prix: dict[str, float], date_livraison: date,
    exception_samedi: bool = False,
) -> bool:
    """Envoie un message de confirmation après mise à jour Pricing live."""
    if not bot_token or not chat_id:
        return False
    lignes = ["✅ <b>Prix d'achat enregistrés</b>", ""]
    for c in ("E85", "SP95-E10", "SP98", "Gazole"):
        if c in prix:
            lignes.append(f"  • {c} : <b>{prix[c]:.3f} € HT</b>".replace(".", ","))
    lignes.append("")
    suffix = " ⚠️ avec supplément samedi" if exception_samedi else ""
    lignes.append(f"📦 Livraison prévue : <b>{date_livraison.strftime('%A %d/%m')}</b>{suffix}")
    message = "\n".join(lignes)
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=bot_token) + "/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.ok
    except requests.RequestException:
        return False
