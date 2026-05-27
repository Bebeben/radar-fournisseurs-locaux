"""
gsheet_io
=========

Lecture / écriture du Google Sheet "Pricing carburants Les Pieux" via
l'API Google Sheets v4. Mirroir de :mod:`xlsx_io` mais avec un backend
cloud — utilisable depuis Claude Code Routines, GitHub Actions, etc.

Authentification : service account JSON (cf. GUIDE_MIGRATION_CLOUD.md).

Onglets gérés (mêmes noms et structure que xlsx_io) :
- **Stations**     : lecture seule (Benjamin l'édite à la main)
- **Concurrents**  : append-only à chaque run
- **Reco prix**    : append-only à chaque run
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.moteur_decision import PrixCarburants, Proposition

log = logging.getLogger(__name__)


class GSheetIO:
    """Wrapper Google Sheets API. Même interface que :class:`XlsxIO`."""

    def __init__(self, sheet_id: str, credentials_path: str | Path):
        if not sheet_id or "A_COMPLETER" in sheet_id:
            raise ValueError(f"sheet_id invalide ou non rempli : {sheet_id!r}")
        creds_path = Path(credentials_path)
        if not creds_path.is_file():
            raise FileNotFoundError(
                f"Credentials Google Sheets introuvables : {creds_path}\n"
                "Voir GUIDE_MIGRATION_CLOUD.md §2.4."
            )
        self.sheet_id = sheet_id
        self.credentials_path = creds_path
        self._service = self._build_service()

    def _build_service(self):
        """Construit le client Sheets API avec le service account."""
        try:
            from google.oauth2.service_account import Credentials  # type: ignore
            from googleapiclient.discovery import build  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "google-api-python-client manquant — `pip install -r requirements.txt`"
            ) from e

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(
            str(self.credentials_path), scopes=scopes
        )
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    # ------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------

    def lire_stations(self) -> list[dict[str, Any]]:
        """Lit l'onglet Stations et renvoie une liste de dicts.

        Note sur le champ ``alignement_actif`` (col F) :
        - ``"OUI"`` / ``"NON"`` : alignement complet ou aucun (tous les carburants)
        - ``"Gazole,E10"`` ou autre liste : alignement PARTIEL sur ces carburants seulement
          (utile pour garde-fou image prix vs un autre Super U sur certains carburants)

        Renvoie ``alignement_carburants`` (list[str]) avec la liste des carburants
        sur lesquels la station entre dans le calcul d'alignement. Vide = jamais.
        """
        rows = self._read_range("Stations!A2:G")
        stations: list[dict[str, Any]] = []
        for row in rows:
            if not row or len(row) < 1 or not str(row[0]).strip():
                continue
            align_raw = str(row[5]).strip() if len(row) > 5 else ""
            align_carbs = _parse_alignement(align_raw)
            stations.append({
                "type": str(row[0]).strip(),
                "nom": str(row[1]).strip() if len(row) > 1 else "",
                "id_prix_carburants": str(row[2]).strip() if len(row) > 2 else "",
                "code_postal": str(row[3]).strip() if len(row) > 3 else "",
                "distance_km": float(row[4]) if len(row) > 4 and row[4] not in ("", None) else 0.0,
                "alignement_actif": bool(align_carbs),  # True si au moins 1 carburant aligné
                "alignement_carburants": align_carbs,  # NEW : liste explicite
                "notes": str(row[6]) if len(row) > 6 else "",
            })
        return stations

    def lire_parametres(self) -> dict:
        """Lit l'onglet Paramètres du Sheet et renvoie les seuils + le mix de vente.

        Layout attendu (cohérent avec le template) :
        - Ligne 7  col C : TVA applicable (ex "20,00%" → 0.20)
        - Ligne 10 col C : Marge cible cts/L (ex "2,0" → 2.0)
        - Ligne 11 col C : Marge plancher cts/L (ex "1,0" → 1.0)
        - Ligne 17 cols C-F : Mix de vente % par carburant
                              (SP95 / SP95-E10 / SP98 / Gazole)

        Returns:
            Dict avec clés ``tva_taux``, ``cible_cts``, ``plancher_cts``, ``mix``
            (mix = dict carburant → ratio 0-1, somme idéalement 1.0).
        """
        from src.moteur_decision import (
            CIBLE_MARGE_CTS_DEFAUT,
            PLANCHER_MARGE_CTS_DEFAUT,
            TVA_FR,
        )

        # Bloc 1 : seuils stratégiques (lignes 7-13 col C)
        # L7=TVA, L10=cible_cts, L11=plancher_cts, L12=cible_ponderee_pct, L13=seuil_tolerance_eur
        rows_seuils = self._read_range("Parametres!C7:C13")

        def cell(rows: list, idx: int) -> str:
            if idx >= len(rows):
                return ""
            r = rows[idx]
            return str(r[0]) if r else ""

        tva = _parse_pourcent_or_float(cell(rows_seuils, 0)) or TVA_FR
        cible = _parse_pourcent_or_float(cell(rows_seuils, 3)) or CIBLE_MARGE_CTS_DEFAUT
        plancher = _parse_pourcent_or_float(cell(rows_seuils, 4)) or PLANCHER_MARGE_CTS_DEFAUT
        # Cible pondérée (ratio 0-1). Lue en C12. Si vide ou invalide → None = pas de cascade.
        cible_pond = _parse_pourcent_or_float(cell(rows_seuils, 5))
        # Seuil tolérance écart prix (€). Lue en C13. Défaut 0.001 si vide.
        seuil_tolerance_eur = _parse_pourcent_or_float(cell(rows_seuils, 6))
        if seuil_tolerance_eur is None or seuil_tolerance_eur <= 0:
            seuil_tolerance_eur = 0.001

        if 0 < cible < 0.5:
            log.warning("Cible suspecte (%.3f), peut-être en ratio au lieu de cts/L", cible)
        if 0 < plancher < 0.5:
            log.warning("Plancher suspect (%.3f), peut-être en ratio au lieu de cts/L", plancher)
        if cible_pond is not None and cible_pond > 1:
            # Si saisi comme "3,5" sans % → normaliser en 0.035
            cible_pond = cible_pond / 100

        # Bloc 2 : mix de vente.
        # Ligne effective = 19 désormais (2 insertions ont poussé tout : cible pondérée L12 + tolérance L13).
        rows_mix = self._read_range("Parametres!C19:F19")
        mix_row = rows_mix[0] if rows_mix else []
        mix_row = list(mix_row) + [None] * (4 - len(mix_row))
        mix = {
            "E85": _parse_pourcent_or_float(mix_row[0]) or 0.0,
            "SP95-E10": _parse_pourcent_or_float(mix_row[1]) or 0.0,
            "SP98": _parse_pourcent_or_float(mix_row[2]) or 0.0,
            "Gazole": _parse_pourcent_or_float(mix_row[3]) or 0.0,
        }
        # Le parser renvoie 0.20 pour "20,00%". Si jamais Benjamin entre "20" sans %,
        # le parser renvoie 20.0 → on normalise.
        for k, v in mix.items():
            if v > 1.0:
                mix[k] = v / 100
        somme = sum(mix.values())
        if somme > 0 and abs(somme - 1.0) > 0.05:
            log.warning(
                "Mix de vente ne somme pas à 100%% (somme=%.2f). Vérifie l'onglet Paramètres ligne 17.",
                somme,
            )

        return {
            "tva_taux": tva,
            "cible_cts": cible,
            "plancher_cts": plancher,
            "cible_ponderee_pct": cible_pond,  # None si cellule vide
            "seuil_tolerance_eur": seuil_tolerance_eur,
            "mix": mix,
        }

    def maj_pricing_live_achat(self, prix_achat_ht: dict[str, float]) -> None:
        """Met à jour les prix d'achat HT dans Pricing live ligne 14 (cols C-F).

        Merge intelligent : pour chaque carburant non fourni dans prix_achat_ht,
        on conserve la valeur existante de Pricing live (pas d'écrasement à vide).
        """
        # Lire les valeurs actuelles pour ne pas écraser ce qui n'est pas dans le dict
        actuel, _ = self.lire_pricing_live()  # actuel = nos_prix_ttc (vente), donc lire_pricing_live renvoie aussi achat
        # En réalité lire_pricing_live renvoie (vente_ttc, achat_ht) — on veut achat
        _, achat_actuel = self.lire_pricing_live()

        def merge(carb, key_actuel):
            v = prix_achat_ht.get(carb)
            return v if v is not None else getattr(achat_actuel, key_actuel)

        row = [
            merge("E85", "sp95"),
            merge("SP95-E10", "sp95_e10"),
            merge("SP98", "sp98"),
            merge("Gazole", "gazole"),
        ]
        clean = [("" if v is None else v) for v in row]
        self._service.spreadsheets().values().update(
            spreadsheetId=self.sheet_id, range="Pricing live!C14:F14",
            valueInputOption="USER_ENTERED",
            body={"values": [clean]}
        ).execute()
        # Mettre aussi à jour C4 (Dernière mise à jour) et C5 (Déclencheur)
        now = datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
        self._service.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {"range": "Pricing live!C4", "values": [[now]]},
                    {"range": "Pricing live!C5", "values": [["Achat confirmé via Telegram"]]},
                ]
            }
        ).execute()

    def maj_pricing_live_vente(
        self,
        prix_vente_ttc: dict[str, float | None],
        declencheur: str = "Telegram",
    ) -> tuple[bool, dict[str, float | None]]:
        """Met à jour les prix de vente TTC dans Pricing live ligne 13 (cols C-F).

        Merge intelligent : pour chaque carburant non fourni (None ou absent du dict),
        on conserve la valeur existante de Pricing live (pas d'écrasement à vide).

        Idempotent : si les prix demandés sont identiques à l'état actuel (dans la
        tolérance _almost_equal), on ne ré-écrit pas et on renvoie a_change=False.

        Args:
            prix_vente_ttc: dict partiel {SP95, SP95-E10, SP98, Gazole} -> float | None.
            declencheur: étiquette pour Pricing live!C5 (ex: "Telegram accepter",
                "Telegram modif partielle").

        Returns:
            (a_change, etat_final) :
              - a_change=True si au moins un prix a réellement été modifié
              - etat_final = dict des 4 carburants après merge (= ce qui est dans le Sheet)
        """
        actuel, _ = self.lire_pricing_live()
        actuels_dict = {
            "E85": actuel.e85,
            "SP95-E10": actuel.sp95_e10,
            "SP98": actuel.sp98,
            "Gazole": actuel.gazole,
        }

        nouveaux = {}
        for k, v_actuel in actuels_dict.items():
            v_demande = prix_vente_ttc.get(k)
            nouveaux[k] = v_demande if v_demande is not None else v_actuel

        a_change = any(
            not _almost_equal(actuels_dict[k], nouveaux[k]) for k in actuels_dict
        )

        if not a_change:
            log.info("maj_pricing_live_vente : aucun changement (prix demandés == actuels)")
            return False, nouveaux

        # Ligne 17 = "Prix de vente TTC affiché (€/L)" dans le template (NOT line 13 qui est header).
        # Ordre cols C-F : C=E85 (ex-SP95) | D=SP95-E10 | E=SP98 | F=Gazole.
        row = [nouveaux["E85"], nouveaux["SP95-E10"], nouveaux["SP98"], nouveaux["Gazole"]]
        clean = [("" if v is None else v) for v in row]
        self._service.spreadsheets().values().update(
            spreadsheetId=self.sheet_id, range="Pricing live!C17:F17",
            valueInputOption="USER_ENTERED",
            body={"values": [clean]},
        ).execute()

        now = datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y %H:%M")
        self._service.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={
                "valueInputOption": "USER_ENTERED",
                "data": [
                    {"range": "Pricing live!C4", "values": [[now]]},
                    {"range": "Pricing live!C5", "values": [[f"Prix vente confirmés - {declencheur}"]]},
                ]
            }
        ).execute()
        return True, nouveaux

    def lire_derniere_proposition_action(self) -> dict[str, float | None] | None:
        """Lit la dernière ligne ACTION de Reco prix et renvoie les prix de vente proposés.

        Parcourt les 50 premières lignes (newest-on-top, donc lignes 2-51 = les plus
        récentes), et retourne les prix proposés (cols D-G = SP95/E10/SP98/Gazole) de la
        première ligne dont col C = "ACTION".

        Returns:
            Dict {SP95, SP95-E10, SP98, Gazole} -> prix proposés (vente TTC) ou None
            si aucune ACTION dans les 50 dernières lignes.
        """
        try:
            rows = self._read_range("Reco prix!A2:G51")
        except Exception as e:
            log.warning("Lecture Reco prix échouée pour derniere_proposition_action : %s", e)
            return None

        for row in rows:
            if len(row) < 7:
                continue
            statut = str(row[2]).strip().upper() if row[2] else ""
            if statut != "ACTION":
                continue
            try:
                def _f(v):
                    if v is None or v == "":
                        return None
                    return float(str(v).replace(",", "."))
                return {
                    "E85": _f(row[3]),
                    "SP95-E10": _f(row[4]),
                    "SP98": _f(row[5]),
                    "Gazole": _f(row[6]),
                }
            except (ValueError, IndexError):
                continue
        return None

    def append_commande(self, date_commande, date_livraison, prix_achat_ht: dict, supplement_samedi: bool = False) -> bool:
        """Ajoute une ligne dans l'onglet Commande avec anti-doublon.

        Anti-doublon : si une commande existe déjà avec la même date_commande
        ET les mêmes 4 prix d'achat, on ne réinsère pas (évite les doublons
        si le workflow Lecture reponse 12h00 est trigger plusieurs fois).

        Structure : Date commande | Date livraison | SP95 | E10 | SP98 | Gazole | Notes

        Returns:
            True si ligne ajoutée, False si doublon détecté.
        """
        # Lecture des 10 dernières commandes pour anti-doublon (newest-on-top → ligne 2+)
        try:
            existantes = self._read_range("Commande!A2:G11")
        except Exception as e:
            log.warning("Lecture Commande pour anti-doublon KO : %s", e)
            existantes = []

        date_str = date_commande.isoformat()
        date_str_fr = date_commande.strftime("%d/%m/%Y")
        for ligne in existantes:
            if not ligne:
                continue
            # Col A = date commande (peut être ISO ou DD/MM/YYYY selon format Sheet)
            d_existante = str(ligne[0]).strip() if len(ligne) >= 1 else ""
            if d_existante not in (date_str, date_str_fr):
                continue
            # Comparer les 4 prix (cols C, D, E, F)
            try:
                e_sp95 = float(str(ligne[2]).replace(",", ".")) if len(ligne) >= 3 and ligne[2] not in ("", None) else None
                e_e10 = float(str(ligne[3]).replace(",", ".")) if len(ligne) >= 4 and ligne[3] not in ("", None) else None
                e_sp98 = float(str(ligne[4]).replace(",", ".")) if len(ligne) >= 5 and ligne[4] not in ("", None) else None
                e_gaz = float(str(ligne[5]).replace(",", ".")) if len(ligne) >= 6 and ligne[5] not in ("", None) else None
            except (ValueError, IndexError):
                continue
            if (
                _almost_equal(e_sp95, prix_achat_ht.get("E85"))
                and _almost_equal(e_e10, prix_achat_ht.get("SP95-E10"))
                and _almost_equal(e_sp98, prix_achat_ht.get("SP98"))
                and _almost_equal(e_gaz, prix_achat_ht.get("Gazole"))
            ):
                log.info("Doublon Commande détecté pour %s — pas de réinsertion", date_str)
                return False

        row = [
            date_commande.isoformat(),
            date_livraison.isoformat(),
            prix_achat_ht.get("E85"),
            prix_achat_ht.get("SP95-E10"),
            prix_achat_ht.get("SP98"),
            prix_achat_ht.get("Gazole"),
            "Supplément livraison samedi" if supplement_samedi else "",
        ]
        clean = [("" if v is None else v) for v in row]
        self._insert_top("Commande", [clean])
        return True

    def bot_est_actif(self) -> bool:
        """Lit le master flag Bot actif (Parametres!C20). True si OUI, False si NON ou absent.

        Tous les workflows doivent appeler cette fonction au démarrage et exit immédiatement
        si False. Permet à Benjamin de mettre le bot en pause directement via le Sheet.
        """
        try:
            rows = self._read_range("Parametres!C20")
            if not rows or not rows[0]:
                return True  # défaut : actif si cellule vide
            v = str(rows[0][0]).strip().upper()
            return v in ("OUI", "YES", "TRUE", "1", "VRAI", "ON", "ACTIF")
        except Exception as e:
            log.warning("Lecture Bot actif KO, on assume actif : %s", e)
            return True

    def set_bot_actif(self, actif: bool) -> None:
        """Écrit OUI/NON dans Parametres!C20."""
        valeur = "OUI" if actif else "NON"
        self._service.spreadsheets().values().update(
            spreadsheetId=self.sheet_id, range="Parametres!C20",
            valueInputOption="USER_ENTERED",
            body={"values": [[valeur]]}
        ).execute()

    def lire_derniers_prix_par_concurrent(self) -> dict[str, dict]:
        """Lit les derniers prix relevés pour chaque concurrent (1 ligne par station).

        Onglet Concurrents en newest-on-top : on lit les 50 premières lignes,
        on prend la première occurrence rencontrée pour chaque station (= la
        plus récente).

        Returns:
            Dict {nom_station: {"Gazole": float, "SP95-E10": float, "SP98": float, "E85": float}}
            avec prix float ou None si non disponible.
        """
        try:
            rows = self._read_range("Concurrents!A2:K50")
        except Exception as e:
            log.warning("Lecture Concurrents pour derniers prix KO : %s", e)
            return {}

        # Structure cols (post refactor SP95→E85) :
        # A=Date | B=Heure | C=Station | D=Type | E=Distance | F=Derniere_maj |
        # G=E85 (ex-SP95) | H=SP95-E10 | I=SP98 | J=Gazole | K=(unused)
        derniers: dict[str, dict] = {}
        for row in rows:
            if not row or len(row) < 3:
                continue
            nom = str(row[2]).strip() if len(row) > 2 else ""
            if not nom or nom in derniers:
                continue  # première occurrence rencontrée = la plus récente (newest-on-top)

            def to_float(v):
                if v is None or v == "":
                    return None
                try:
                    return float(str(v).replace(",", "."))
                except ValueError:
                    return None

            derniers[nom] = {
                "E85": to_float(row[6]) if len(row) > 6 else None,
                "SP95-E10": to_float(row[7]) if len(row) > 7 else None,
                "SP98": to_float(row[8]) if len(row) > 8 else None,
                "Gazole": to_float(row[9]) if len(row) > 9 else None,
            }
        return derniers

    def lire_telegram_last_update_id(self) -> int:
        """Lit le dernier update_id Telegram traité depuis Parametres!C25.

        Permet au polling de ne traiter que les messages NOUVEAUX depuis
        le dernier polling, sans dépendre d'une fenêtre temporelle.

        Returns:
            int : dernier update_id traité (0 si vide ou non parseable).
        """
        try:
            rows = self._read_range("Parametres!C25")
            if not rows or not rows[0]:
                return 0
            return int(str(rows[0][0]).strip())
        except (ValueError, Exception) as e:
            log.warning("Lecture last_update_id KO, on assume 0 : %s", e)
            return 0

    def set_telegram_last_update_id(self, update_id: int) -> None:
        """Écrit le dernier update_id Telegram traité dans Parametres!C25."""
        self._service.spreadsheets().values().update(
            spreadsheetId=self.sheet_id, range="Parametres!C25",
            valueInputOption="USER_ENTERED",
            body={"values": [[str(update_id)]]}
        ).execute()

    def lire_heure_bascule(self) -> str:
        """Lit l'heure bascule prix J/J+1 depuis Parametres ligne 19. Défaut 11:00."""
        rows = self._read_range("Parametres!C19")
        if not rows or not rows[0]:
            return "11:00"
        v = str(rows[0][0]).strip()
        return v if v else "11:00"

    def append_prix_achat(
        self,
        date_jour,
        heure_message,
        pour_jour: str,  # "J" ou "J+1"
        prix: dict[str, float],
        source: str,
        variation_anormale: list[str] | None = None,
    ) -> bool:
        """Append une ligne dans Prix d'achat avec anti-doublon.

        Anti-doublon : si une ligne existe déjà pour (date_jour, pour_jour) avec
        EXACTEMENT les mêmes 4 prix, on n'en ajoute pas une nouvelle.

        Returns:
            True si ligne ajoutée, False si doublon détecté.
        """
        # Lire les lignes existantes du jour pour anti-doublon
        existantes = self.lire_prix_achat_du_jour(date_jour, pour_jour)
        for e in existantes:
            if (
                _almost_equal(e.get("E85"), prix.get("E85"))
                and _almost_equal(e.get("SP95-E10"), prix.get("SP95-E10"))
                and _almost_equal(e.get("SP98"), prix.get("SP98"))
                and _almost_equal(e.get("Gazole"), prix.get("Gazole"))
            ):
                return False  # doublon

        var_str = " | ".join(variation_anormale) if variation_anormale else ""
        row = [
            date_jour.isoformat() if hasattr(date_jour, "isoformat") else str(date_jour),
            heure_message.strftime("%H:%M") if hasattr(heure_message, "strftime") else str(heure_message),
            pour_jour,
            prix.get("E85"),
            prix.get("SP95-E10"),
            prix.get("SP98"),
            prix.get("Gazole"),
            source,
            var_str,
        ]
        clean = [("" if v is None else v) for v in row]
        self._insert_top("Prix d'achat", [clean])
        return True

    def lire_prix_achat_du_jour(self, date_jour, pour_jour: str | None = None) -> list[dict]:
        """Lit les lignes de Prix d'achat correspondant à une date (et optionnellement un J/J+1).

        Returns:
            Liste de dicts {Date, Heure, Pour jour, SP95, SP95-E10, SP98, Gazole, Source}.
            Ordre = celui du Sheet (newest-on-top → la plus récente en premier).
        """
        rows = self._read_range("Prix d'achat!A2:I500")
        # Cibler date_jour comme objet date pour comparaison robuste
        from datetime import date as _date
        if not isinstance(date_jour, _date):
            try:
                date_jour = _date.fromisoformat(str(date_jour))
            except ValueError:
                return []

        out = []
        for row in rows:
            if not row or len(row) < 7:
                continue
            row_date_obj = _parse_date_robuste(row[0])
            if row_date_obj != date_jour:
                continue
            row_pj = str(row[2]).strip() if len(row) > 2 else ""
            if pour_jour and row_pj != pour_jour:
                continue
            out.append({
                "Date": row[0], "Heure": row[1] if len(row) > 1 else "",
                "Pour jour": row_pj,
                "E85": _try_float(row[3] if len(row) > 3 else None),
                "SP95-E10": _try_float(row[4] if len(row) > 4 else None),
                "SP98": _try_float(row[5] if len(row) > 5 else None),
                "Gazole": _try_float(row[6] if len(row) > 6 else None),
                "Source": row[7] if len(row) > 7 else "",
            })
        return out

    def lire_dernier_prix_achat(self) -> dict[str, float] | None:
        """Renvoie le dernier prix d'achat enregistré (toutes balises confondues).

        Sert à la détection de variation anormale.
        """
        rows = self._read_range("Prix d'achat!A2:I2")  # ligne 2 = la plus récente (newest-on-top)
        if not rows or not rows[0] or len(rows[0]) < 7:
            return None
        row = rows[0]
        return {
            "E85": _try_float(row[3] if len(row) > 3 else None),
            "SP95-E10": _try_float(row[4] if len(row) > 4 else None),
            "SP98": _try_float(row[5] if len(row) > 5 else None),
            "Gazole": _try_float(row[6] if len(row) > 6 else None),
        }

    def lire_parametres_livraison(self) -> dict:
        """Lit les params livraison (lignes 16-18 de Parametres)."""
        rows = self._read_range("Parametres!C16:C18")

        def cell(idx: int):
            if idx >= len(rows):
                return None
            r = rows[idx]
            return r[0] if r else None

        delai_raw = cell(0)
        try:
            delai = int(delai_raw) if delai_raw else 1
        except (ValueError, TypeError):
            delai = 1

        samedi_raw = str(cell(1) or "NON").strip().upper()
        samedi_possible = samedi_raw in ("OUI", "YES", "TRUE", "1")

        return {"delai_jours": delai, "samedi_possible": samedi_possible}

    def lire_marge_ponderee(self) -> float | None:
        """Lit la marge pondérée déjà calculée par les formules du template.

        Cellule attendue : Pricing live!C24 (taux de marque pondéré, ex "4,39%").
        Si la cellule est vide ou non parsable → None.

        Note : on lit le résultat NATIF du Sheet (formule existante du template),
        on ne recalcule pas en Python — ça respecte les éventuels ajustements
        que Benjamin ferait à la formule du Sheet.
        """
        rows = self._read_range("Pricing live!C24")
        if not rows or not rows[0]:
            return None
        return _parse_pourcent_or_float(rows[0][0])

    def lire_pricing_live(self) -> tuple[PrixCarburants, PrixCarburants]:
        """Lit l'onglet Pricing live et renvoie (vente_ttc, achat_ht).

        Layout attendu (cohérent avec le template `commande_prix_station_v2.xlsx`) :
        - Ligne 13 colonnes C-F : headers carburants (SP95, SP95-E10, SP98, Gazole)
        - Ligne 14 colonnes C-F : prix d'achat HT
        - Ligne 17 colonnes C-F : prix de vente TTC

        Les valeurs peuvent être au format ``"1,368 €"`` (str) ou ``1.368`` (number)
        selon comment Benjamin les entre dans le Sheet — on parse les deux.
        """
        rows = self._read_range("Pricing live!C13:F17")
        # rows[0] = headers, rows[1] = achat HT, rows[4] = vente TTC
        if len(rows) < 5:
            log.warning("Pricing live trop court (lignes attendues 13-17), valeurs None")
            return PrixCarburants(), PrixCarburants()

        achat_row = rows[1] if len(rows) > 1 else []
        vente_row = rows[4] if len(rows) > 4 else []

        achat = _parse_pricing_row(achat_row)
        vente = _parse_pricing_row(vente_row)
        return vente, achat

    def lire_reco_prix_historique(self, nb_lignes: int = 50) -> list[dict]:
        """Lit les N premières lignes de Reco prix (sert au verrou intra-journée).

        Note : depuis le passage à insertion newest-on-top, les lignes les plus
        récentes sont en haut. Donc nb_lignes = on prend les nb_lignes premières
        après le header.

        Range borné (A2:C500) pour éviter HttpError 400 quand le sheet vient d'être
        vidé (grille minimale = 1 ligne header).
        """
        try:
            rows = self._read_range("Reco prix!A2:C500")
        except Exception as e:
            log.warning("Lecture Reco prix échouée (sheet probablement vide) : %s", e)
            return []
        lignes: list[dict] = []
        for row in rows:
            if not row or not str(row[0]).strip():
                continue
            lignes.append({
                "Date": str(row[0]) if len(row) > 0 else "",
                "Heure run": str(row[1]) if len(row) > 1 else "",
                "Statut": str(row[2]) if len(row) > 2 else "",
            })
        return lignes[:nb_lignes]

    # ------------------------------------------------------------
    # Écriture
    # ------------------------------------------------------------

    def lire_derniers_prix_par_concurrent(self) -> dict[str, dict[str, float | None]]:
        """Lit l'onglet Concurrents et renvoie le dernier relevé par station.

        Concurrents est en newest-on-top. On parcourt depuis le haut, on garde
        le premier hit par nom de station (= le plus récent pour cette station).

        Structure col : A=Date B=Heure C=Nom D=Type E=Dist F=DerniereMaj
                        G=SP95 H=SP95-E10 I=SP98 J=Gazole K=E85 L=Notes

        Returns:
            Dict {nom_station: {SP95, SP95-E10, SP98, Gazole}} avec prix float ou None.
            Vide si Concurrents est vide.
        """
        try:
            rows = self._read_range("Concurrents!A2:L1000")
        except Exception as e:
            log.warning("Lecture Concurrents KO : %s", e)
            return {}

        derniers: dict[str, dict[str, float | None]] = {}
        for row in rows:
            if len(row) < 10:
                continue
            nom = str(row[2]).strip() if row[2] else ""
            if not nom or nom in derniers:
                continue  # déjà vu (= ligne plus récente déjà gardée)

            def _f(v):
                if v is None or v == "":
                    return None
                try:
                    return float(str(v).replace(",", "."))
                except ValueError:
                    return None

            derniers[nom] = {
                "E85": _f(row[6]),
                "SP95-E10": _f(row[7]),
                "SP98": _f(row[8]),
                "Gazole": _f(row[9]),
            }
        return derniers

    def append_concurrents(
        self,
        prix_par_station: dict[str, PrixCarburants],
        stations_meta: list[dict[str, Any]],
        prix_e85_par_station: dict[str, float | None],
        derniere_maj_par_station: dict[str, str | None],
        maintenant: datetime,
    ) -> int:
        """Append 1 ligne par station relevée dans l'onglet Concurrents.

        Args:
            derniere_maj_par_station: id_station -> horodatage formaté du dernier
                changement de prix relevé par l'API ("27/04 07:30").
        """
        rows: list[list[Any]] = []
        for meta in stations_meta:
            id_st = meta["id_prix_carburants"]
            if id_st not in prix_par_station:
                continue
            pc = prix_par_station[id_st]
            rows.append([
                maintenant.date().isoformat(),
                maintenant.strftime("%H:%M"),
                meta["nom"],
                meta["type"],
                meta["distance_km"],
                derniere_maj_par_station.get(id_st, ""),
                pc.e85,
                pc.sp95_e10,
                pc.sp98,
                pc.gazole,
                prix_e85_par_station.get(id_st),
                meta.get("notes", ""),
            ])
        if rows:
            self._insert_top("Concurrents", rows)
        return len(rows)

    def append_reco_prix(
        self,
        proposition: Proposition,
        nos_prix_ttc: PrixCarburants,
        prix_recommandes_ttc: dict[str, float],
        marge_ponderee: float | None,
        maintenant: datetime,
    ) -> None:
        """Append 1 ligne dans l'onglet Reco prix.

        Args:
            marge_ponderee: marge pondérée globale (ratio 0-1, ex 0.0439 pour 4,39%)
                lue depuis Pricing live!C24 (formule native du template).
        """
        from src.moteur_decision import Action

        if proposition.action == Action.ACTION and prix_recommandes_ttc:
            prix_finaux = prix_recommandes_ttc
        else:
            prix_finaux = {
                "E85": nos_prix_ttc.e85,
                "SP95-E10": nos_prix_ttc.sp95_e10,
                "SP98": nos_prix_ttc.sp98,
                "Gazole": nos_prix_ttc.gazole,
            }

        marges_pct = {
            "E85": _pct(proposition.marges.get("E85")),
            "SP95-E10": _pct(proposition.marges.get("SP95-E10")),
            "SP98": _pct(proposition.marges.get("SP98")),
            "Gazole": _pct(proposition.marges.get("Gazole")),
        }

        ligne = [
            maintenant.date().isoformat(),
            maintenant.strftime("%H:%M"),
            proposition.action.value,
            prix_finaux.get("E85"),
            prix_finaux.get("SP95-E10"),
            prix_finaux.get("SP98"),
            prix_finaux.get("Gazole"),
            marges_pct["E85"],
            marges_pct["SP95-E10"],
            marges_pct["SP98"],
            marges_pct["Gazole"],
            marge_ponderee,
            proposition.niveau_cascade if proposition.niveau_cascade else "",  # col M = Niveau cascade (1/2/3/4 ou vide)
            proposition.justification,
        ]
        self._insert_top("Reco prix", [ligne])

    # ------------------------------------------------------------
    # Helpers API
    # ------------------------------------------------------------

    def _read_range(self, range_a1: str) -> list[list[Any]]:
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range=range_a1)
            .execute()
        )
        return result.get("values", [])

    def _append_range(self, range_a1: str, rows: list[list[Any]]) -> None:
        # Convertir None en "" (l'API Sheets ne sérialise pas None correctement)
        clean_rows = [[("" if v is None else v) for v in row] for row in rows]
        body = {"values": clean_rows}
        self._service.spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=range_a1,
            valueInputOption="USER_ENTERED",  # interprète les nombres comme nombres
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()

    def _insert_top(self, sheet_name: str, rows: list[list[Any]]) -> None:
        """Insert N lignes en position 2 (juste après le header).

        Les lignes existantes sont poussées vers le bas. Les plus récentes
        restent ainsi toujours visibles en haut, sans scroll.

        Cas particulier : si le sheet ne contient que le header (1 ligne),
        on ne peut pas insérer "avant" car la grille est trop petite.
        Dans ce cas, on append simplement (= la ligne devient la ligne 2,
        ce qui revient au même que "insertion en haut").
        """
        if not rows:
            return
        n = len(rows)
        meta = self._service.spreadsheets().get(spreadsheetId=self.sheet_id).execute()
        sheet_meta = next(
            s for s in meta["sheets"] if s["properties"]["title"] == sheet_name
        )
        sheet_id = sheet_meta["properties"]["sheetId"]
        grid_rows = sheet_meta["properties"]["gridProperties"]["rowCount"]

        clean_rows = [[("" if v is None else v) for v in row] for row in rows]

        # Si la grille a au plus 1 ligne (= juste header), on fait un append simple
        # car insertDimension ne peut pas pousser une grille de taille 1.
        if grid_rows <= 1:
            self._append_range(f"'{sheet_name}'!A:A", clean_rows)
            return

        # Cas standard : insérer n lignes vides en position 2 (push existing down)
        self._service.spreadsheets().batchUpdate(
            spreadsheetId=self.sheet_id,
            body={"requests": [{
                "insertDimension": {
                    "range": {"sheetId": sheet_id, "dimension": "ROWS",
                              "startIndex": 1, "endIndex": 1 + n},
                    "inheritFromBefore": False,
                }
            }]}
        ).execute()

        # Écrire les données dans les nouvelles lignes
        self._service.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=f"'{sheet_name}'!A2",
            valueInputOption="USER_ENTERED",
            body={"values": clean_rows}
        ).execute()


# ============================================================
# Helpers
# ============================================================


def _parse_date_robuste(val: Any):
    """Parse une date depuis plusieurs formats : DD/MM/YYYY, YYYY-MM-DD, serial Excel.

    Returns:
        ``datetime.date`` ou None si rien ne matche.
    """
    # date/datetime/timedelta sont importés via le top-level (cf. en-tête du fichier)
    from datetime import date, timedelta  # date n'est pas top-level, timedelta non plus
    if val is None or val == "":
        return None
    # Cas 1 : déjà un objet date
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    # Cas 2 : serial Excel (entier ou float — jours depuis 1899-12-30)
    if isinstance(val, (int, float)):
        try:
            base = date(1899, 12, 30)
            return base + timedelta(days=int(val))
        except (ValueError, OverflowError):
            return None
    # Cas 3 : string — essayer plusieurs formats
    s = str(val).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _try_float(val: Any) -> float | None:
    """Essaie de convertir en float (gère strings avec virgule)."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", ".").replace("€", "").replace(" ", "")
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _almost_equal(a: float | None, b: float | None, tol: float = 0.0001) -> bool:
    """Compare 2 floats avec tolérance, gérant les None."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) < tol


def _parse_pourcent_or_float(val: Any) -> float | None:
    """Parse une valeur Sheet : '20,00%' → 0.20, '2,0' → 2.0, '15 km' → 15.0."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    is_percent = s.endswith("%")
    # Nettoyer : %, €, km, espaces (incl. insécables), virgule → point
    for noise in ("%", "€", "km", " ", "\xa0"):
        s = s.replace(noise, "")
    s = s.replace(",", ".").strip()
    try:
        f = float(s)
        return f / 100 if is_percent else f
    except ValueError:
        return None


def _parse_pricing_row(row: list[Any]) -> PrixCarburants:
    """Parse une ligne de 4 valeurs (ordre SP95, SP95-E10, SP98, Gazole).

    Accepte :
    - nombres (1.368)
    - chaînes au format français ("1,368 €", "1,368", "1.368", etc.)
    - cellules vides → None
    """
    def to_float(val: Any) -> float | None:
        if val is None or val == "":
            return None
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip()
        # Nettoyer : retirer "€", espaces (y compris insécables), remplacer virgule par point
        s = s.replace("€", "").replace(" ", "").replace("\xa0", "").strip()
        s = s.replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    # Pad la liste si moins de 4 éléments
    # Ordre des colonnes dans Pricing live!C-F après refactor SP95→E85 :
    # C=E85 (ex-SP95) | D=SP95-E10 | E=SP98 | F=Gazole
    row = list(row) + [None] * (4 - len(row))
    return PrixCarburants(
        e85=to_float(row[0]),
        sp95_e10=to_float(row[1]),
        sp98=to_float(row[2]),
        gazole=to_float(row[3]),
    )


def _parse_alignement(val: Any) -> list[str]:
    """Parse le champ 'Alignement actif' de Stations.

    Accepte :
    - "OUI" / "YES" / "TRUE" / "VRAI" → tous les carburants
    - "NON" / "NO" / "FALSE" / vide → aucun
    - "Gazole,E10" ou "Gazole, E10" ou "Gazole | E10" → liste partielle
    """
    if val is None or val == "":
        return []
    s = str(val).strip().upper()
    if s in ("OUI", "YES", "TRUE", "1", "VRAI", "ON"):
        return ["E85", "SP95-E10", "SP98", "Gazole"]
    if s in ("NON", "NO", "FALSE", "0", "FAUX", "OFF"):
        return []
    # Liste de carburants — split sur , ; |
    import re as _re
    tokens = _re.split(r"[,;|]", val)
    out = []
    for t in tokens:
        n = t.strip().upper().replace(" ", "")
        # Normalisation noms
        if n in ("SP95-E10", "E10", "SP95E10"):
            out.append("SP95-E10")
        elif n == "E85":
            out.append("E85")
        elif n == "SP98":
            out.append("SP98")
        elif n in ("GAZOLE", "GAZ", "DIESEL"):
            out.append("Gazole")
    return out


def _parse_bool(val: Any) -> bool:
    if val is None:
        return False
    s = str(val).strip().upper()
    return s in ("OUI", "YES", "TRUE", "1", "VRAI", "X")


def _pct(marge: Any) -> float | None:
    if marge is None:
        return None
    return getattr(marge, "marge_pourcent", None)
