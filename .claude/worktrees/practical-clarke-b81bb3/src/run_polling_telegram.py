"""
run_polling_telegram
====================

Polling Telegram léger toutes les 5 min (lun-sam 8h-13h Paris) pour lire et
traiter les commandes envoyées par Benjamin en quasi-temps-réel.

Commandes reconnues :
    - "pause" / "pause bot" / "/pause"          → toggle bot à NON (BYPASS master flag)
    - "reprise" / "reprendre" / "/reprise" / "/start" / "go"
                                                → toggle bot à OUI (BYPASS master flag)
    - "prix E85=... E10=... SP98=... GAZ=..."  → enregistre prix d'achat HT (anti-doublon)
                                                  ack confirmé avec lien Sheet
    - "prix vente E85=... E10=..."             → maj partielle prix de vente TTC (Pricing live!C13:F13)
                                                  ack confirmé avec lien Sheet
    - "accept" / "accepter" / "ok"              → applique tous les prix proposés par le
                                                  dernier run ACTION (depuis Reco prix col D-G)
                                                  ack confirmé avec lien Sheet

Master flag : si bot en pause, seule la commande "reprise" est traitée.
Idempotence : pas d'ack si action sans effet (anti-doublon append, valeurs identiques, etc.).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from src.achat_telegram import (
    determiner_pour_jour,
    detecter_variation_anormale,
    envoyer_photo_telegram,
    est_commande_accept,
    est_commande_aide,
    est_commande_modifier,
    lien_sheet,
    lire_messages_telegram,
    parser_message_prix,
    parser_message_prix_vente,
)

log = logging.getLogger("polling_telegram")
TELEGRAM_API = "https://api.telegram.org/bot{token}"


def _envoyer_visuel_post_application(
    backend, bot_token: str, chat_id: str, sheet_id: str,
    prix_avant: dict, prix_appliques: dict, maintenant_paris,
) -> None:
    """Envoie une 2e photo confirmant l'application : AVANT vs NOUVEAUX prix.

    Recalcule la marge pondérée APRÈS application pour la footer du visuel,
    pour que Benjamin voie immédiatement l'impact de sa décision.

    Utilise les derniers prix concurrents stockés dans Concurrents (newest-on-top).
    Ne re-appelle PAS l'API gouv (économie + cohérence avec le dernier run).
    Toute exception est catchée → confirmation texte déjà envoyée par le caller.
    """
    try:
        from src.visuel_concurrence import generer_png_concurrence
        from src.moteur_decision import PrixCarburants, simuler_marge_ponderee

        # Lecture concurrents depuis Sheet
        concurrents_data = backend.lire_derniers_prix_par_concurrent()
        stations = backend.lire_stations()
        # Distance + alignements pour chaque concurrent
        prix_concurrents_visuel = {}
        alignements = {}
        for s in stations:
            nom = s["nom"]
            if nom in concurrents_data and s["type"].lower() != "reference":
                prix_concurrents_visuel[nom] = concurrents_data[nom]
                if s.get("alignement_carburants"):
                    alignements[nom] = s["alignement_carburants"]
        # Nom + marge
        nom_nous = next((s["nom"] for s in stations if s["type"].lower() == "reference"), "Notre station")
        params = backend.lire_parametres() if hasattr(backend, "lire_parametres") else {}

        # Recalculer la marge ponderee APRES application
        marge_apres = None
        try:
            _, prix_achat_ht = backend.lire_pricing_live()
            prix_vente_apres = PrixCarburants(
                gazole=prix_appliques.get("Gazole") or prix_avant.get("Gazole"),
                sp95_e10=prix_appliques.get("SP95-E10") or prix_avant.get("SP95-E10"),
                sp98=prix_appliques.get("SP98") or prix_avant.get("SP98"),
                e85=prix_appliques.get("E85") or prix_avant.get("E85"),
            )
            marge_apres = simuler_marge_ponderee(
                prix_vente_apres, prix_achat_ht,
                params.get("mix", {}), params.get("tva_taux", 0.20),
            )
            log.info("Marge ponderee APRES application : %.4f", marge_apres or 0)
        except Exception as e:
            log.warning("Calcul marge APRES KO : %s", e)

        png = generer_png_concurrence(
            prix_concurrents=prix_concurrents_visuel,
            nos_prix=prix_avant,
            nom_nous=nom_nous,
            marge_ponderee=marge_apres,  # marge APRES application
            marge_cible=params.get("cible_ponderee_pct"),
            maintenant=maintenant_paris,
            alignements_partiels=alignements,
            prix_appliques=prix_appliques,
        )
        envoyer_photo_telegram(bot_token, chat_id, png,
            caption="✅ <b>Prix appliqués</b> dans Pricing live (Sheet à jour)"
        )
        log.info("Visuel post-application envoyé")
    except Exception as e:
        log.warning("Echec envoi visuel post-application : %s", e)


def _envoyer(bot_token: str, chat_id: str, texte: str) -> None:
    if not bot_token or not chat_id:
        return
    try:
        requests.post(
            TELEGRAM_API.format(token=bot_token) + "/sendMessage",
            json={
                "chat_id": chat_id, "text": texte,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except requests.RequestException:
        pass


def _fmt_prix(p: float | None) -> str:
    if p is None:
        return "—"
    return f"{p:.3f} €".replace(".", ",")


def run() -> int:
    racine = Path(__file__).resolve().parent.parent
    env_file = racine / ".env"
    if env_file.is_file():
        load_dotenv(env_file)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    sheet_id = os.getenv("GSHEET_ID", "")
    if not bot_token or not chat_id:
        log.error("Telegram secrets manquants")
        return 1

    paris = ZoneInfo("Europe/Paris")
    maintenant = datetime.now(paris)

    # Backend Sheet : nécessaire dès le début pour lire le last_update_id
    from src.gsheet_io import GSheetIO
    creds_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials/google_sheets_sa.json")
    if not Path(creds_path).is_absolute():
        creds_path = str(racine / creds_path)
    backend = GSheetIO(sheet_id=sheet_id, credentials_path=creds_path)

    def _get_backend():
        return backend

    # Lecture des messages : on utilise l'offset = last_update_id + 1 pour ne traiter
    # QUE les messages qui n'ont pas encore été traités. Aucun message rate, peu importe
    # la fréquence du polling. last_update_id stocké dans Parametres!C25.
    last_update_id = backend.lire_telegram_last_update_id()
    offset = last_update_id + 1 if last_update_id > 0 else None
    messages = lire_messages_telegram(bot_token, chat_id, offset=offset)
    if not messages:
        log.info("Aucun nouveau message (last_update_id=%d)", last_update_id)
        return 0
    log.info("%d message(s) à traiter (offset=%s)", len(messages), offset)

    # Trier les messages chronologiquement (plus ancien d'abord)
    messages_tries = sorted(messages, key=lambda m: m["date"])

    # Bot en pause ? On lit le flag avant de traiter quoi que ce soit
    # (pour décider si on traite ou pas les commandes hors reprise/pause)
    bot_actif = _get_backend().bot_est_actif()

    nb_traites = 0
    for msg in messages_tries:
        text = msg["text"]
        text_lower = text.lower().strip()
        # msg["date"] est aware UTC (cf. lire_messages_telegram) → convertir en Paris
        msg_dt = msg["date"].astimezone(paris) if msg["date"].tzinfo else msg["date"].replace(tzinfo=paris)

        # ----------------------------------------------------------------
        # 0. Aide / Liste des commandes (BYPASS master flag, toujours dispo)
        # ----------------------------------------------------------------
        if est_commande_aide(text):
            _envoyer(bot_token, chat_id, _msg_aide(sheet_id))
            log.info("Aide envoyée")
            nb_traites += 1
            continue

        # ----------------------------------------------------------------
        # 1. Pause / Reprise (BYPASS master flag, toujours traitées)
        # ----------------------------------------------------------------
        if text_lower in ("pause", "pause bot", "/pause"):
            if not bot_actif:
                log.info("'pause' reçue mais bot déjà en pause — skip")
                continue
            _get_backend().set_bot_actif(False)
            bot_actif = False
            _envoyer(bot_token, chat_id,
                "🔕 <b>Bot mis en pause</b>\n\n"
                "Pour reprendre :\n"
                "• Tape <code>reprise</code> ici (latence ~5 min)\n"
                "• Ou Sheet → Parametres → C20 → OUI (effet immédiat)"
            )
            log.info("Bot toggle à NON")
            nb_traites += 1
            continue

        if text_lower in ("reprise", "reprendre", "/reprise", "/start", "go"):
            if bot_actif:
                log.info("'reprise' reçue mais bot déjà actif — skip")
                continue
            _get_backend().set_bot_actif(True)
            bot_actif = True
            _envoyer(bot_token, chat_id,
                "✅ <b>Bot relancé</b>\n\nTous les workflows reprennent normalement."
            )
            log.info("Bot toggle à OUI")
            nb_traites += 1
            continue

        # ----------------------------------------------------------------
        # Si bot en pause, on ignore tout le reste (sauf pause/reprise déjà traitées)
        # ----------------------------------------------------------------
        if not bot_actif:
            log.debug("Bot en pause, message ignoré : %r", text[:40])
            continue

        # ----------------------------------------------------------------
        # 1.5 Commande "Modifier prix" : suggère le format de saisie
        # ----------------------------------------------------------------
        if est_commande_modifier(text):
            _envoyer(bot_token, chat_id,
                "✏️ <b>Saisis tes prix de vente TTC</b> au format :\n\n"
                "<code>prix vente Gazole=2,209 E10=2,049 SP98=2,149 E85=0,819</code>\n\n"
                "Tu peux ne mettre que 1 ou 2 carburants si tu veux modifier juste ceux-là."
            )
            nb_traites += 1
            continue

        # ----------------------------------------------------------------
        # 2. Prix de VENTE (commande: "prix vente E85=...")
        #    Test AVANT prix d'achat car même préfixe "prix"
        # ----------------------------------------------------------------
        prix_vente = parser_message_prix_vente(text)
        if prix_vente:
            be = _get_backend()
            # Capter prix AVANT pour la photo post-application
            vente_avant, _ = be.lire_pricing_live()
            prix_avant_dict = vente_avant.as_dict()
            a_change, etat_final = be.maj_pricing_live_vente(
                prix_vente, declencheur=f"Telegram modif {msg_dt.strftime('%H:%M')}"
            )
            if a_change:
                _envoyer(bot_token, chat_id, _msg_ack_prix_vente(etat_final, sheet_id, partiel=True))
                _envoyer_visuel_post_application(
                    be, bot_token, chat_id, sheet_id,
                    prix_avant=prix_avant_dict, prix_appliques=etat_final,
                    maintenant_paris=msg_dt,
                )
                log.info("Prix vente mis à jour : %s", etat_final)
                nb_traites += 1
            else:
                log.info("Prix vente demandés == actuels — skip ack (idempotent)")
            continue

        # ----------------------------------------------------------------
        # 3. Accepter prix proposés (commande: "accepter")
        # ----------------------------------------------------------------
        if est_commande_accept(text):
            be = _get_backend()
            proposition = be.lire_derniere_proposition_action()
            if not proposition:
                _envoyer(bot_token, chat_id,
                    "⚠️ <b>Aucune proposition ACTION récente trouvée.</b>\n\n"
                    "Le bot ne propose des nouveaux prix que sur les runs ACTION "
                    "(quand l'écart vs concurrents > tolérance). "
                    "Si tu veux fixer les prix manuellement, tape :\n"
                    "<code>prix vente E85=1,649 E10=... SP98=... GAZ=...</code>"
                )
                continue
            # Capter prix AVANT pour la photo post-application
            vente_avant, _ = be.lire_pricing_live()
            prix_avant_dict = vente_avant.as_dict()
            a_change, etat_final = be.maj_pricing_live_vente(
                proposition, declencheur=f"Telegram accepter {msg_dt.strftime('%H:%M')}"
            )
            if a_change:
                _envoyer(bot_token, chat_id, _msg_ack_prix_vente(etat_final, sheet_id, partiel=False))
                _envoyer_visuel_post_application(
                    be, bot_token, chat_id, sheet_id,
                    prix_avant=prix_avant_dict, prix_appliques=etat_final,
                    maintenant_paris=msg_dt,
                )
                log.info("Prix vente acceptés (proposition) : %s", etat_final)
                nb_traites += 1
            else:
                log.info("'accepter' reçue mais prix vente == proposition (déjà appliquée)")
                _envoyer(bot_token, chat_id,
                    "ℹ️ <b>Prix de vente déjà alignés sur la proposition.</b>\n\n"
                    "Aucune modification nécessaire dans Pricing live."
                )
            continue

        # ----------------------------------------------------------------
        # 3.5 Fallback "intention d'application" non reconnue.
        # Si le message contient des mots-clefs comme "ok", "aligne", "applique",
        # "valide" mais n'a pas match est_commande_accept (variante trop libre),
        # on suggere les commandes valides au lieu d'ignorer silencieusement.
        # ----------------------------------------------------------------
        mots = set(text_lower.replace(",", " ").split())
        intentions = {"aligne", "aligner", "alignement", "applique", "appliquer",
                      "valide", "valider", "accept", "accepter"}
        if mots & intentions and not parser_message_prix(text) and not parser_message_prix_vente(text):
            _envoyer(bot_token, chat_id,
                "💡 <b>Tu veux appliquer les prix proposés ?</b>\n\n"
                "Tape exactement <code>ok</code> ou <code>accepter</code> ou <code>appliquer recos</code>.\n\n"
                "Ou modifie partiellement :\n"
                "<code>prix vente Gazole=2,209 E10=2,049</code>"
            )
            log.info("Intention d'application détectée mais non parsable : %r", text[:80])
            nb_traites += 1
            continue

        # ----------------------------------------------------------------
        # 4. Prix d'ACHAT (commande: "prix E85=..." sans "vente")
        # ----------------------------------------------------------------
        prix_achat = parser_message_prix(text)
        # Si le message commence par "prix" mais aucun carburant reconnu :
        # envoyer un message d'aide au lieu d'un skip silencieux.
        if prix_achat is None and text_lower.startswith("prix") and not text_lower.startswith("prix vente"):
            _envoyer(bot_token, chat_id,
                "⚠️ <b>Format prix non reconnu</b>\n\n"
                "Tape par ex :\n"
                "<code>prix Gazole=1,950 E10=1,580 SP98=1,720 E85=0,580</code>\n\n"
                "Ou partiel (1 seul carburant) :\n"
                "<code>prix gazole=1,950</code>\n\n"
                "Alias acceptés : <code>GAZ</code> / <code>GAZOLE</code> / <code>GASOIL</code> / <code>DIESEL</code>, "
                "<code>E10</code> / <code>SP95-E10</code>, <code>SP98</code>, <code>E85</code>."
            )
            log.info("Message 'prix ...' non reconnu : %r", text[:80])
            nb_traites += 1
            continue
        if prix_achat:
            be = _get_backend()
            heure_bascule = be.lire_heure_bascule()
            pour_jour = determiner_pour_jour(msg_dt, heure_bascule)
            precedent = be.lire_dernier_prix_achat()
            alertes = detecter_variation_anormale(prix_achat, precedent, seuil_eur=0.10)

            ajoute = be.append_prix_achat(
                date_jour=msg_dt.date(),
                heure_message=msg_dt,
                pour_jour=pour_jour,
                prix=prix_achat,
                source="Telegram polling",
                variation_anormale=alertes,
            )
            if ajoute:
                _envoyer(bot_token, chat_id, _msg_ack_prix_achat(prix_achat, pour_jour, alertes, sheet_id))
                log.info("Prix achat enregistré : pour=%s %s", pour_jour, prix_achat)
                nb_traites += 1
            else:
                log.info("Prix achat doublon (déjà ack précédemment) — skip")
            continue

        # ----------------------------------------------------------------
        # 5. FALLBACK : message non reconnu par AUCUN parser.
        # On répond explicitement pour ne pas laisser Benjamin sans feedback.
        # EXCEPTIONS : "oui"/"non" sont géres par run_lecture_reponse 12h30
        # (réponse question achat), pas par le polling. Pour ces 2 mots on
        # reste silencieux pour ne pas créer de double notif.
        # ----------------------------------------------------------------
        if text_lower in ("oui", "non", "oui samedi", "oui force"):
            log.info("Message 'oui/non' : géré par run_lecture_reponse, polling silencieux")
            continue
        # Commandes manifestement destinées à un autre bot (commence par /)
        if text_lower.startswith("/") and not est_commande_aide(text):
            log.info("Commande slash non reconnue (autre bot ?) : %r", text[:50])
            continue
        # Tout le reste : message d'erreur explicite
        _envoyer(bot_token, chat_id,
            f"❓ <b>Je n'ai pas compris</b> ton message :\n"
            f"<i>{text[:100]}</i>\n\n"
            "Tape <code>aide</code> pour voir la liste des commandes disponibles."
        )
        log.info("Message non reconnu, fallback envoyé : %r", text[:80])
        nb_traites += 1

    # Mettre à jour le last_update_id avec le max des messages traités
    # (même si non-traité comme commande, ils ont été "vus" → on n'y revient pas).
    max_update_id = max((m.get("update_id", 0) for m in messages), default=last_update_id)
    if max_update_id > last_update_id:
        backend.set_telegram_last_update_id(max_update_id)
        log.info("last_update_id : %d -> %d", last_update_id, max_update_id)

    log.info("Polling fini. %d commande(s) traitée(s) sur %d message(s) lus.",
             nb_traites, len(messages))
    return 0


def _msg_aide(sheet_id: str) -> str:
    """Construit le message d'aide listant toutes les commandes Telegram disponibles."""
    lignes = [
        "🤖 <b>Commandes disponibles</b>",
        "",
        "<b>━ État du bot ━</b>",
        "<code>pause</code> — mettre le bot en pause",
        "<code>reprise</code> — relancer le bot",
        "",
        "<b>━ Prix d'ACHAT HT (à tout moment) ━</b>",
        "<code>prix GAZ=1,950 E10=1,580 SP98=1,720 E85=0,799</code>",
        "<code>prix gaz=2,15</code> (saisie partielle aussi acceptée)",
        "→ Enregistré balise J ou J+1 selon l'heure (bascule à 11h)",
        "",
        "<b>━ Réponse question 11h30 ━</b>",
        "<code>oui</code> — on commande, prends les derniers prix J",
        "<code>non</code> — pas de commande aujourd'hui",
        "<code>oui samedi</code> — exception livraison samedi (vendredi seulement)",
        "<code>oui force</code> — bypass sanity check",
        "",
        "<b>━ Prix de VENTE TTC (après run ACTION 8h) ━</b>",
        "<code>accepter</code> — applique tous les prix proposés",
        "<code>prix vente E85=1,649</code> — override partiel",
        "<code>prix vente E85=1,649 GAZ=1,948</code> — plusieurs carb",
        "",
        "<b>━ Aide ━</b>",
        "<code>aide</code> / <code>commandes</code> / <code>?</code> — afficher ce menu",
    ]
    url = lien_sheet(sheet_id)
    if url:
        lignes.append("")
        lignes.append(f'📊 <a href="{url}">Ouvrir le Sheet</a>')
    return "\n".join(lignes)


def _msg_ack_prix_vente(prix: dict[str, float | None], sheet_id: str, partiel: bool) -> str:
    """Construit l'ack Telegram pour une maj prix de vente."""
    titre = "Prix de vente mis à jour" + (" (modif partielle)" if partiel else " (proposition acceptée)")
    lignes = [
        f"✅ <b>{titre}</b>",
        "",
        "<b>Pricing live :</b>",
        f"  • SP95   : {_fmt_prix(prix.get('E85'))}",
        f"  • SP95-E10 : {_fmt_prix(prix.get('SP95-E10'))}",
        f"  • SP98   : {_fmt_prix(prix.get('SP98'))}",
        f"  • Gazole : {_fmt_prix(prix.get('Gazole'))}",
        "",
        "👉 À déclarer sur prix-carburants.gouv.fr",
    ]
    url = lien_sheet(sheet_id)
    if url:
        lignes.append("")
        lignes.append(f'📊 <a href="{url}">Voir le Sheet</a>')
    return "\n".join(lignes)


def _msg_ack_prix_achat(prix: dict[str, float], pour_jour: str, alertes: list[str], sheet_id: str) -> str:
    """Construit l'ack Telegram pour une saisie prix d'achat."""
    lignes = [
        f"✅ <b>Prix d'achat enregistré</b> (balise <b>{pour_jour}</b>)",
        "",
    ]
    for k_label, k_dict in [("E85", "E85"), ("E10", "SP95-E10"), ("SP98", "SP98"), ("Gazole", "Gazole")]:
        v = prix.get(k_dict)
        if v is not None:
            lignes.append(f"  • {k_label} : {_fmt_prix(v)} HT")
    if alertes:
        lignes.append("")
        lignes.append("⚠️ <b>Variation anormale détectée :</b>")
        for a in alertes:
            lignes.append(f"  • {a}")
    url = lien_sheet(sheet_id)
    if url:
        lignes.append("")
        lignes.append(f'📊 <a href="{url}">Voir le Sheet</a>')
    return "\n".join(lignes)


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
        log.error("CRASH run_polling_telegram : %s", e, exc_info=True)
        _notifier_crash_telegram(e, contexte="polling Telegram")
        sys.exit(1)
    sys.exit(code)
