"""
sheet_io
========

Lecture / écriture du Google Sheet "Commande et prix station v2" (5 onglets).

En DRY_RUN : pas d'appel Google Sheets, utilise un état mocké en mémoire
ou chargé depuis une fixture JSON.

En production : utilise l'API Google Sheets via google-api-python-client
avec un service account. Implémentation complète à finaliser lors
du déploiement (nécessite un compte de service Google Cloud + partage du Sheet).

Onglets attendus (noms exacts) :
1. Pricing live    : état courant des prix
2. Historique      : append-only, une ligne par run
3. Commande        : livraisons carburant
4. Transport       : coûts transport
5. Paramètres      : cibles, planchers, seuils
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from src.moteur_decision import PrixCarburants, Proposition

log = logging.getLogger(__name__)

# Colonnes de l'onglet Historique (ordre de la ligne écrite)
COLONNES_HISTORIQUE = [
    "Date",
    "Heure run",
    "Déclencheur",
    "E85 Achat HT",
    "SP95-E10 Achat HT",
    "SP98 Achat HT",
    "Gazole Achat HT",
    "E85 Vente TTC",
    "SP95-E10 Vente TTC",
    "SP98 Vente TTC",
    "Gazole Vente TTC",
    "Marge SP95 cts/L",
    "Marge SP95-E10 cts/L",
    "Marge SP98 cts/L",
    "Marge Gazole cts/L",
    "Statut",
    "Commentaire",
]


class SheetIO:
    """Wrapper autour de l'API Google Sheets, avec bascule DRY_RUN."""

    def __init__(
        self,
        sheet_id: str,
        *,
        dry_run: bool = True,
        fixture_path: str | Path | None = None,
        credentials_path: str | Path | None = None,
    ):
        self.sheet_id = sheet_id
        self.dry_run = dry_run
        self.fixture_path = Path(fixture_path) if fixture_path else None
        self.credentials_path = Path(credentials_path) if credentials_path else None

        # État mocké en DRY_RUN
        self._etat_mock: dict[str, Any] = {}
        if self.dry_run and self.fixture_path and self.fixture_path.is_file():
            with self.fixture_path.open(encoding="utf-8") as f:
                self._etat_mock = json.load(f)

    # ------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------

    def lire_pricing_live(self) -> tuple[PrixCarburants, PrixCarburants]:
        """Renvoie (nos_prix_ttc, prix_achat_ht) depuis l'onglet Pricing live."""
        if self.dry_run:
            data = self._etat_mock.get("pricing_live", {})
            achat = data.get("achat_ht", {})
            vente = data.get("vente_ttc", {})
            return (
                PrixCarburants(
                    gazole=vente.get("Gazole"),
                    sp95_e10=vente.get("SP95-E10"),
                    sp98=vente.get("SP98"),
                    e85=vente.get("E85"),
                ),
                PrixCarburants(
                    gazole=achat.get("Gazole"),
                    sp95_e10=achat.get("SP95-E10"),
                    sp98=achat.get("SP98"),
                    e85=achat.get("E85"),
                ),
            )

        log.info("[TODO] Lecture Pricing live via Sheets API — à finaliser")
        return PrixCarburants(), PrixCarburants()

    def lire_historique(self, nb_lignes: int = 50) -> list[dict]:
        """Renvoie les N dernières lignes de l'onglet Historique."""
        if self.dry_run:
            return self._etat_mock.get("historique", [])[-nb_lignes:]
        log.info("[TODO] Lecture Historique via Sheets API — à finaliser")
        return []

    def lire_parametres(self) -> dict[str, Any]:
        """Renvoie les paramètres depuis l'onglet Paramètres (cibles, planchers)."""
        if self.dry_run:
            return self._etat_mock.get("parametres", {})
        log.info("[TODO] Lecture Paramètres via Sheets API — à finaliser")
        return {}

    # ------------------------------------------------------------
    # Écriture
    # ------------------------------------------------------------

    def append_historique(
        self,
        proposition: Proposition,
        nos_prix_ttc: PrixCarburants,
        prix_achat_ht: PrixCarburants,
        declencheur: str,
        maintenant: datetime,
    ) -> None:
        """Ajoute une ligne à l'onglet Historique."""
        ligne = {
            "Date": maintenant.date().isoformat(),
            "Heure run": maintenant.strftime("%H:%M"),
            "Déclencheur": declencheur,
            "E85 Achat HT": prix_achat_ht.e85,
            "SP95-E10 Achat HT": prix_achat_ht.sp95_e10,
            "SP98 Achat HT": prix_achat_ht.sp98,
            "Gazole Achat HT": prix_achat_ht.gazole,
            "E85 Vente TTC": nos_prix_ttc.e85,
            "SP95-E10 Vente TTC": nos_prix_ttc.sp95_e10,
            "SP98 Vente TTC": nos_prix_ttc.sp98,
            "Gazole Vente TTC": nos_prix_ttc.gazole,
            "Marge SP95 cts/L": proposition.marges.get("E85").marge_cts_litre if proposition.marges.get("E85") else None,
            "Marge SP95-E10 cts/L": proposition.marges.get("SP95-E10").marge_cts_litre if proposition.marges.get("SP95-E10") else None,
            "Marge SP98 cts/L": proposition.marges.get("SP98").marge_cts_litre if proposition.marges.get("SP98") else None,
            "Marge Gazole cts/L": proposition.marges.get("Gazole").marge_cts_litre if proposition.marges.get("Gazole") else None,
            "Statut": proposition.action.value,
            "Commentaire": proposition.justification,
        }

        if self.dry_run:
            self._etat_mock.setdefault("historique", []).append(ligne)
            log.info("[DRY_RUN] Ligne ajoutée à Historique (mémoire seule) : %s", proposition.action.value)
            return

        log.info("[TODO] Append Historique via Sheets API — à finaliser")

    # ------------------------------------------------------------
    # Utilitaires
    # ------------------------------------------------------------

    def etat_mock(self) -> dict[str, Any]:
        """Retourne l'état mocké courant (DRY_RUN) — utile pour tests et logs."""
        return self._etat_mock
