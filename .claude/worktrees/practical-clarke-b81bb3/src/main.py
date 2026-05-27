"""
main
====

Orchestrateur du run pricing carburants. Enchaîne :

    1. Chargement .env + configs YAML
    2. Vérification jour actif (pas de run le dimanche)
    3. Lecture Sheet (Pricing live, Historique, Paramètres)
    4. Détection verrou intra-journée
    5. Scan Gmail pour nouvelle facture
    6. Appel API prix-carburants pour concurrents
    7. Calcul décision (moteur_decision)
    8. Construction mail (mail_builder) si ACTION/INFO
    9. Envoi mail (ou log en DRY_RUN)
    10. Append historique

Usage :
    DRY_RUN=true python -m src.main
    python -m src.main      # utilise .env

Exit codes :
    0 : run OK (qu'il y ait eu mail ou pas)
    1 : erreur config (placeholders en prod, YAML mal formé, etc.)
    2 : jour inactif (dimanche) — cas normal, pas une erreur
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from src.api_carburants import recuperer_derniere_maj, recuperer_prix_stations
from src.config_loader import (
    load_all,
    resumer_placeholders,
    valider_pour_production,
)
from src.gmail_factures import detecter_nouvelle_facture
from src.mail_builder import construire_mail
from src.notifier import notifier_telegram
from src.moteur_decision import (
    Action,
    PrixCarburants,
    est_jour_actif,
    proposer_repricing,
    verrou_intra_journee_actif,
)
from src.sheet_io import SheetIO
from src.xlsx_io import XlsxIO

# Le backend cloud (gsheet) est importé en lazy pour éviter d'imposer
# google-api-python-client en mode local xlsx.

log = logging.getLogger("pricing")


def _bool_env(nom: str, defaut: bool = False) -> bool:
    val = os.getenv(nom, "").strip().lower()
    if not val:
        return defaut
    return val in ("true", "1", "yes", "oui", "on")


def _setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _detecter_changements_concurrents(
    anciens: dict[str, dict],
    nouveaux: dict[str, dict],
    seuil_eur: float = 0.001,
) -> list[str]:
    """Compare prix concurrents anciens (lus avant append) vs nouveaux (relevé du run).

    Args:
        anciens : {nom_station: {Gazole, SP95-E10, SP98, E85}} avec prix float ou None.
        nouveaux : idem.
        seuil_eur : écart minimum pour considérer un "vrai" changement (défaut 0,001 €).

    Returns:
        Liste de strings descriptives, vide si aucun changement >= seuil.
        Ex : ["Intermarché Les Pieux Gazole : 1,945 → 1,932 (-1,3 cts)"]
    """
    changements = []
    for nom, prix_n in nouveaux.items():
        prix_a = anciens.get(nom, {})
        for carb, p_n in prix_n.items():
            p_a = prix_a.get(carb)
            if p_n is None and p_a is None:
                continue
            if p_n is None or p_a is None:
                changements.append(f"{nom} {carb}: nouveau prix ou disparu")
                continue
            ecart = p_n - p_a
            if abs(ecart) >= seuil_eur:
                signe = "+" if ecart > 0 else ""
                changements.append(
                    f"{nom} {carb}: {p_a:.3f} → {p_n:.3f} ({signe}{ecart * 100:.1f}c)".replace(".", ",")
                )
    return changements


def run() -> int:
    # Racine du projet (..)
    racine = Path(__file__).resolve().parent.parent

    # 1. Charger .env
    env_file = racine / ".env"
    if env_file.is_file():
        load_dotenv(env_file)
    else:
        load_dotenv(racine / ".env.example")  # fallback pour dry-run initial
        log.info("Pas de .env, fallback sur .env.example pour dry-run")

    dry_run = _bool_env("DRY_RUN", True)
    mock_api = _bool_env("MOCK_API", dry_run)
    mail_actif = _bool_env("MAIL_ACTIF", False)  # désactivé tant que pas de magasin
    log_level = os.getenv("LOG_LEVEL", "INFO")
    _setup_logging(log_level)

    log.info("=" * 60)
    log.info(
        "Pricing carburants Les Pieux — %s | mail=%s",
        "DRY_RUN" if dry_run else "PRODUCTION",
        "ON" if mail_actif else "OFF",
    )
    log.info("=" * 60)

    # Master flag : si bot en pause, exit silencieux
    from src.bot_status import bot_actif_ou_skip
    if not bot_actif_ou_skip(racine):
        return 0

    # 2. Charger configs (interne au projet par défaut, surchargeable via env var)
    config_dir = Path(os.getenv("CONFIG_DIR", "config"))
    if not config_dir.is_absolute():
        config_dir = (racine / config_dir).resolve()
    # Fallback historique : si ./config n'existe pas, essayer ../config
    if not config_dir.is_dir():
        fallback = (racine.parent / "config").resolve()
        if fallback.is_dir():
            config_dir = fallback

    try:
        configs, placeholders = load_all(config_dir)
    except Exception as e:
        log.error("Impossible de charger les configs : %s", e)
        return 1

    if placeholders:
        log.info(resumer_placeholders(placeholders))
        if not dry_run:
            try:
                valider_pour_production(placeholders)
            except RuntimeError as e:
                log.error(str(e))
                return 1
        else:
            log.info("DRY_RUN : placeholders tolérés, on continue.")

    # 3. Vérifier jour actif + heure attendue (filtrage côté Python pour gérer DST)
    paris = ZoneInfo("Europe/Paris")
    maintenant = datetime.now(paris)
    weekend_actif = configs["stations_config"]["parametres"].get("weekend_actif", False)
    if not est_jour_actif(maintenant.isoweekday(), weekend_actif):
        log.info("Dimanche ou weekend désactivé. Aucun run. Bye.")
        return 0  # skip volontaire = succès (le workflow CI doit rester vert)

    # NB : ancien filtre RUN_HEURES_PARIS retiré.
    # Cron-job.org pilote l'heure désormais. Filtre côté Python = piège
    # silencieux qui skippait les triggers manuels et le pricing_check.
    # Cf. tasks/lessons.md entrée du 2026-04-29.

    # 4. Choix du backend (xlsx local ou Google Sheet cloud) et lecture stations
    backend_nom = os.getenv("BACKEND", "xlsx").lower()
    backend = _ouvrir_backend(backend_nom, racine)
    stations_meta = backend.lire_stations()
    log.info("Backend=%s — Stations lues : %d entrées", backend_nom, len(stations_meta))

    # Lecture pricing live : depuis le backend (gsheet OU xlsx) si dispo,
    # sinon fallback sur fixture JSON (utile en dev local sans Sheet).
    if hasattr(backend, "lire_pricing_live"):
        nos_prix_ttc, prix_achat_ht = backend.lire_pricing_live()
        source = f"backend {backend_nom} (onglet 'Pricing live')"
    else:
        sheet_id = configs["parametres_magasin"]["google_drive"]["sheet_pricing_id"]
        fixture_sheet = racine / "tests" / "fixtures" / "sheet_state_exemple.json"
        sheet = SheetIO(
            sheet_id=sheet_id,
            dry_run=True,
            fixture_path=fixture_sheet,
        )
        nos_prix_ttc, prix_achat_ht = sheet.lire_pricing_live()
        source = "fixture JSON (fallback)"
    log.info("Pricing live lu depuis %s : vente %s | achat %s", source, nos_prix_ttc.as_dict(), prix_achat_ht.as_dict())

    # 5. Détection verrou (lecture historique des reco précédentes dans xlsx)
    historique = backend.lire_reco_prix_historique()
    verrou = verrou_intra_journee_actif(historique, maintenant.date())
    log.info("Verrou intra-journée actif : %s", verrou)

    # 6. Scan Gmail pour nouvelle facture (no-op si MAIL_ACTIF=false ou pas de credentials)
    gmail_conf = configs["parametres_gmail"]
    nouvelle_facture = False
    if mail_actif:
        facture = detecter_nouvelle_facture(
            libelle=gmail_conf["filtrage_factures_gmail"]["libelle"],
            expediteurs_attendus=gmail_conf["filtrage_factures_gmail"]["expediteurs_attendus"],
            date_derniere_check=maintenant.date(),
            dry_run=dry_run,
            fixture_path=None,
            credentials_path=os.getenv("GMAIL_CREDENTIALS_PATH"),
        )
        nouvelle_facture = facture is not None
        if nouvelle_facture:
            log.info("Nouvelle facture détectée : %s", facture)
            prix_achat_ht = facture.prix_achat_ht

    # 7. Récupérer prix concurrents via API (depuis xlsx Stations)
    ids_a_relever = [
        s["id_prix_carburants"]
        for s in stations_meta
        if s["id_prix_carburants"] and "A_COMPLETER" not in s["id_prix_carburants"]
    ]
    fixture_api = racine / "tests" / "fixtures" / "reponse_api_exemple.json"
    prix_stations = recuperer_prix_stations(
        ids_a_relever,
        mock=mock_api,
        chemin_fixture=fixture_api if mock_api else None,
    )
    # E85 est maintenant dans le mainflow via PrixCarburants.e85 (pas besoin d'appel séparé).
    derniere_maj = recuperer_derniere_maj(
        ids_a_relever,
        mock=mock_api,
        chemin_fixture=fixture_api if mock_api else None,
    )
    log.info("Prix récupérés pour %d station(s)", len(prix_stations))

    # Append à l'onglet Concurrents (1 ligne par station relevée, sauf notre station)
    concurrents_meta = [s for s in stations_meta if s["type"].lower() != "reference"]

    # On lit TOUJOURS les anciens prix concurrents AVANT l'append, indépendamment
    # de NOTIFIER_SI_CHANGEMENT. Permet de :
    #   - skip la notif TEXTE en mode pricing_check si rien n'a bougé (NOTIFIER_SI_CHANGEMENT)
    #   - décider d'envoyer le visuel UNIQUEMENT si changement concurrent (Inter/Bricquebec)
    notifier_si_changement = os.getenv("NOTIFIER_SI_CHANGEMENT", "false").lower() == "true"
    anciens_prix_concurrents: dict[str, dict] = {}
    if hasattr(backend, "lire_derniers_prix_par_concurrent"):
        try:
            anciens_prix_concurrents = backend.lire_derniers_prix_par_concurrent()
            log.info("Anciens prix concurrents lus : %d concurrents",
                     len(anciens_prix_concurrents))
        except Exception as e:
            log.warning("Lecture anciens prix concurrents KO : %s", e)

    nb_lignes = backend.append_concurrents(prix_stations, concurrents_meta, {}, derniere_maj, maintenant)
    log.info("Onglet Concurrents : %d ligne(s) ajoutée(s)", nb_lignes)

    # Mapping nom -> prix pour le moteur (concurrents avec au moins 1 carburant aligné)
    concurrents_principaux = {
        s["nom"]: prix_stations[s["id_prix_carburants"]]
        for s in stations_meta
        if s.get("alignement_actif") and s["id_prix_carburants"] in prix_stations
    }
    # Mapping nom -> liste de carburants alignés (pour alignement partiel type Bricquebec Gazole/E10)
    concurrents_carburants = {
        s["nom"]: s.get("alignement_carburants", [])
        for s in stations_meta
        if s.get("alignement_actif") and s["id_prix_carburants"] in prix_stations
    }

    # 7a-bis. SELF-CHECK : comparer les prix de NOTRE station via l'API
    # vs ce qu'on a dans Pricing live (= ce que Benjamin a saisi).
    # Si écart → Benjamin n'a peut-être pas appliqué la reco de la veille
    # (ou a oublié de mettre à jour soit la caisse, soit Pricing live).
    self_check_alertes: list[str] = []
    station_ref = next((s for s in stations_meta if s["type"].lower() == "reference"), None)
    if station_ref and station_ref["id_prix_carburants"] in prix_stations:
        prix_api_self = prix_stations[station_ref["id_prix_carburants"]]
        from src.moteur_decision import CARBURANTS as _CARBS
        for c in _CARBS:
            api_p = prix_api_self.get(c)  # type: ignore[arg-type]
            sheet_p = nos_prix_ttc.get(c)  # type: ignore[arg-type]
            if api_p is None or sheet_p is None:
                continue
            ecart_eur = abs(api_p - sheet_p)
            if ecart_eur > 0.001:  # plus de 0.1 ct d'écart → suspect
                self_check_alertes.append(
                    f"{c} : Pricing live {sheet_p:.3f} € vs prix-carburants.gouv.fr {api_p:.3f} € "
                    f"(écart {ecart_eur * 100:.1f} cts)".replace(".", ",")
                )
    if self_check_alertes:
        log.warning("Self-check API : %d écart(s) avec Pricing live", len(self_check_alertes))
        for a in self_check_alertes:
            log.warning("  - %s", a)

    # 7b. Lire les seuils stratégiques depuis l'onglet Paramètres du Sheet (si dispo)
    if hasattr(backend, "lire_parametres"):
        params = backend.lire_parametres()
        cible_pond = params.get("cible_ponderee_pct")
        cible_pond_str = f"{cible_pond * 100:.2f}%" if cible_pond is not None else "non définie"
        log.info(
            "Paramètres lus du Sheet : cible=%.2f cts/L | plancher=%.2f cts/L | cible pondérée=%s | TVA=%.2f",
            params["cible_cts"], params["plancher_cts"], cible_pond_str, params["tva_taux"],
        )
    else:
        from src.moteur_decision import (
            CIBLE_MARGE_CTS_DEFAUT,
            PLANCHER_MARGE_CTS_DEFAUT,
            TVA_FR,
        )
        params = {
            "cible_cts": CIBLE_MARGE_CTS_DEFAUT,
            "plancher_cts": PLANCHER_MARGE_CTS_DEFAUT,
            "tva_taux": TVA_FR,
            "cible_ponderee_pct": None,
            "mix": {},
        }

    # 8. Décision (avec les seuils dynamiques + cascade pondérée + tolérance + multi-carb)
    proposition = proposer_repricing(
        nos_prix_ttc,
        prix_achat_ht,
        concurrents_principaux,
        verrou_actif=verrou,
        nouvelle_facture=nouvelle_facture,
        mouvements_surveillance=[],
        cible_cts=params["cible_cts"],
        plancher_cts=params["plancher_cts"],
        cible_ponderee_pct=params.get("cible_ponderee_pct"),
        mix=params.get("mix"),
        tva_taux=params["tva_taux"],
        seuil_tolerance_eur=params.get("seuil_tolerance_eur", 0.001),
        concurrents_carburants=concurrents_carburants,
    )
    if proposition.niveau_cascade:
        log.info("Cascade niveau %d activé", proposition.niveau_cascade)

    # Si self-check a détecté des écarts → préfixer la justification pour que Benjamin le voie
    if self_check_alertes:
        from dataclasses import replace
        prefix = (
            f"⚠️ Self-check ({len(self_check_alertes)} écart) : "
            + " ; ".join(self_check_alertes)
            + " — vérifie si tu as bien appliqué la reco de la veille (caisse + déclaration gouv.fr)\n\n"
        )
        proposition = replace(proposition, justification=prefix + proposition.justification)
    log.info("Décision : %s — %s", proposition.action.value, proposition.justification)

    # 8b. Lire la marge pondérée déjà calculée par les formules de Pricing live
    marge_ponderee = None
    if hasattr(backend, "lire_marge_ponderee"):
        marge_ponderee = backend.lire_marge_ponderee()
        if marge_ponderee is not None:
            log.info("Marge pondérée lue depuis Pricing live!C24 : %.2f %%", marge_ponderee * 100)

    # 8c. Append à l'onglet Reco prix (TOUJOURS, même en SILENCE — traçabilité complète)
    backend.append_reco_prix(proposition, nos_prix_ttc, proposition.nouveaux_prix, marge_ponderee, maintenant)
    log.info("Onglet 'Reco prix' : 1 ligne ajoutée (statut=%s)", proposition.action.value)

    # 8d. Notification Telegram (push mobile) — toujours envoyé (même STATU QUO,
    # = "le run a tourné, voici l'état du marché et tes marges")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    sheet_id = os.getenv("GSHEET_ID", "")

    # On calcule TOUJOURS les changements (pour decider d'envoyer le visuel),
    # mais le skip_notif_telegram (TEXTE) ne s'active qu'en mode NOTIFIER_SI_CHANGEMENT.
    skip_notif_telegram = False
    nouveaux_par_concurrent = {}
    for s in concurrents_meta:
        id_st = s["id_prix_carburants"]
        if id_st in prix_stations:
            pc = prix_stations[id_st]
            nouveaux_par_concurrent[s["nom"]] = {
                "Gazole": pc.gazole, "SP95-E10": pc.sp95_e10,
                "SP98": pc.sp98, "E85": pc.e85,
            }
    changements = _detecter_changements_concurrents(
        anciens_prix_concurrents, nouveaux_par_concurrent, seuil_eur=0.001
    )
    if changements:
        log.info("Changement(s) concurrent détecté(s) : %s", changements)
    else:
        log.info("Aucun changement concurrent vs derniere ligne")
    if notifier_si_changement:
        if proposition.action.value == "ACTION":
            log.info("Statut ACTION → notif TEXTE envoyée (peu importe les changements)")
        elif changements:
            log.info("Changement détecté → notif TEXTE envoyée")
        else:
            log.info("Aucun changement + pas d'ACTION → notif TEXTE skippée")
            skip_notif_telegram = True

    if not skip_notif_telegram:
        notifier_telegram(
            proposition,
            nos_prix_ttc.as_dict(),
            marge_ponderee,
            maintenant,
            bot_token=bot_token,
            chat_id=chat_id,
            sheet_id=sheet_id,
        )

    # 8e. Envoi du visuel "relevé concurrence" — déclenché UNIQUEMENT si
    # changement détecté chez Inter ou Bricquebec vs run précédent.
    # Pas de spam : si rien n'a bougé chez les concurrents, pas de visuel.
    # Override possible via NOTIFIER_VISUEL_TOUJOURS=true (debug).
    notifier_visuel = bool(bot_token and chat_id) and (
        bool(changements)
        or os.getenv("NOTIFIER_VISUEL_TOUJOURS", "false").lower() == "true"
    )
    if notifier_visuel:
        try:
            from src.visuel_concurrence import generer_png_concurrence
            from src.achat_telegram import envoyer_photo_telegram
            # Construction du dict prix_concurrents pour le visuel :
            # {nom_station: {Gazole, SP95-E10, SP98, E85, derniere_maj_api}}
            prix_concurrents_visuel: dict[str, dict] = {}
            for s in concurrents_meta:
                id_st = s["id_prix_carburants"]
                if id_st not in prix_stations:
                    continue
                pc = prix_stations[id_st]
                prix_concurrents_visuel[s["nom"]] = {
                    "Gazole": pc.gazole,
                    "SP95-E10": pc.sp95_e10,
                    "SP98": pc.sp98,
                    "E85": pc.e85,
                    "derniere_maj_api": derniere_maj.get(id_st, ""),
                }
            # Alignements partiels (ex Bricquebec sur Gazole+E10 seulement)
            alignements = {
                s["nom"]: s.get("alignement_carburants", [])
                for s in concurrents_meta
                if s.get("alignement_actif")
            }
            png = generer_png_concurrence(
                prix_concurrents=prix_concurrents_visuel,
                nos_prix=nos_prix_ttc.as_dict(),
                nom_nous=station_ref["nom"] if station_ref else "Notre station",
                marge_ponderee=marge_ponderee,
                marge_cible=params.get("cible_ponderee_pct"),
                maintenant=maintenant,
                alignements_partiels=alignements,
            )
            envoyer_photo_telegram(
                bot_token, chat_id, png,
                caption=f"📍 Relevé concurrence — statut <b>{proposition.action.value}</b>"
            )
            log.info("Visuel concurrence envoyé sur Telegram (statut=%s)", proposition.action.value)
        except Exception as e:
            log.warning("Echec génération/envoi visuel Telegram : %s", e)

    # 9. Construction + envoi mail (sauf si MAIL_ACTIF=false)
    if not mail_actif:
        log.info("MAIL_ACTIF=false : étape mail désactivée. Décision affichée plus haut.")
    else:
        mail = construire_mail(
            proposition,
            nos_prix_ttc.as_dict(),
            gmail_conf,
            maintenant,
            dry_run=dry_run,
            sheet_url=None,  # remplir en prod avec URL du Sheet
        )

        if mail is None:
            log.info("Pas de mail (SILENCE).")
        else:
            log.info("Mail prêt — sujet : %s", mail.sujet)
            if dry_run:
                log.info("[DRY_RUN] Corps du mail simulé :\n%s", mail.corps)
            else:
                _envoyer_mail_reel(mail)

    # 10. Plus d'historique technique — l'onglet "Reco prix" du xlsx fait office d'archive
    log.info("Run terminé.")
    return 0


def _nommer_declencheur(maintenant: datetime, proposition, nouvelle_facture: bool) -> str:
    if nouvelle_facture:
        return "Post-livraison"
    if proposition.action == Action.ACTION:
        return f"Run {maintenant.hour}h — ACTION"
    if proposition.action == Action.INFO:
        return f"Run {maintenant.hour}h — INFO"
    return f"Run {maintenant.hour}h — statu quo"


def _envoyer_mail_reel(mail) -> None:
    """Envoi réel via Gmail API — à finaliser lors du déploiement."""
    log.info("[TODO] Envoi Gmail API — à finaliser. Sujet : %s", mail.sujet)


def _ouvrir_backend(nom: str, racine: Path):
    """Ouvre le backend de stockage selon BACKEND=xlsx|gsheet.

    - xlsx   : fichier Excel local à la racine du pack de specs
    - gsheet : Google Sheet via API (nécessite GSHEET_ID + credentials)
    """
    if nom == "xlsx":
        xlsx_path = racine.parent / "commande_prix_station_v2.xlsx"
        if not xlsx_path.is_file():
            raise FileNotFoundError(f"xlsx introuvable : {xlsx_path}")
        return XlsxIO(xlsx_path)

    if nom == "gsheet":
        # Lazy import pour ne pas imposer google-api-python-client en mode xlsx
        from src.gsheet_io import GSheetIO

        sheet_id = os.getenv("GSHEET_ID", "")
        creds_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials/google_sheets_sa.json")
        if not Path(creds_path).is_absolute():
            creds_path = str(racine / creds_path)
        return GSheetIO(sheet_id=sheet_id, credentials_path=creds_path)

    raise ValueError(f"BACKEND inconnu : {nom!r}. Valeurs attendues : xlsx | gsheet")


def _notifier_crash_telegram(exception: Exception, contexte: str = "main.py") -> None:
    """Envoie une alerte Telegram si main.py crash. Évite les crashs silencieux
    sur GitHub Actions où Benjamin ne consulte pas systématiquement les logs."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return
    try:
        import traceback
        import requests
        tb_short = "\n".join(traceback.format_exc().splitlines()[-8:])  # 8 dernières lignes
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
        pass  # ne pas masquer l'erreur originale par un crash dans le notifier


if __name__ == "__main__":
    try:
        code = run()
    except Exception as e:
        log.error("CRASH main.py : %s", e, exc_info=True)
        _notifier_crash_telegram(e, contexte="pricing main.py")
        sys.exit(1)
    sys.exit(code)
