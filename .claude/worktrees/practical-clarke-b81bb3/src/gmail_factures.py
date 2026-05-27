"""
gmail_factures
==============

Scanne le libellé Gmail "Factures/Carburant" pour détecter les nouvelles
factures fournisseur et extraire les prix d'achat HT.

En DRY_RUN : ne fait aucun appel réel, renvoie une facture mockée
si une fixture est fournie, sinon renvoie None.

En production : utilise l'API Gmail via google-api-python-client.
L'implémentation complète sera finalisée au moment du déploiement
(nécessite OAuth setup + parsing spécifique au format factures SIPLEC).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from src.moteur_decision import PrixCarburants

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Facture:
    """Une facture fournisseur détectée et parsée."""

    date_livraison: date
    fournisseur: str
    prix_achat_ht: PrixCarburants
    reference: str  # N° facture ou Message-Id Gmail


def detecter_nouvelle_facture(
    libelle: str,
    expediteurs_attendus: list[str],
    date_derniere_check: date,
    *,
    dry_run: bool = False,
    fixture_path: str | Path | None = None,
    credentials_path: str | Path | None = None,
) -> Facture | None:
    """Cherche une facture non encore traitée dans Gmail.

    Args:
        libelle: nom du libellé Gmail (ex. "Factures/Carburant").
        expediteurs_attendus: liste blanche des adresses reconnues.
        date_derniere_check: on ignore les factures antérieures à cette date.
        dry_run: si True, pas d'appel Gmail réel ; utilise ``fixture_path``.
        fixture_path: JSON mockée {"fournisseur": ..., "date": ..., "prix_achat_ht": {...}}.
        credentials_path: OAuth token Gmail (production).

    Returns:
        :class:`Facture` si une nouvelle est détectée, sinon None.
    """
    if dry_run:
        return _charger_fixture_facture(fixture_path)

    # Production : importer dynamiquement google-api-python-client
    # (évite de faire crasher DRY_RUN si la lib n'est pas installée)
    try:
        from googleapiclient.discovery import build  # type: ignore
        from google.oauth2.credentials import Credentials  # type: ignore
    except ImportError:
        log.error("google-api-python-client non installé — `pip install -r requirements.txt`")
        return None

    if credentials_path is None or not Path(credentials_path).is_file():
        log.error("Credentials Gmail introuvables : %s", credentials_path)
        return None

    log.info("[TODO] Appel Gmail API réel — à finaliser lors du déploiement")
    # Implémentation complète à coder lors de la prise de fonction :
    # 1. Charger Credentials depuis credentials_path (OAuth token)
    # 2. service = build('gmail', 'v1', credentials=creds)
    # 3. Lister messages avec query : f'label:{libelle} after:{date_derniere_check}'
    # 4. Pour chaque message : vérifier expéditeur dans expediteurs_attendus
    # 5. Parser le corps HTML/texte pour extraire prix HT par carburant
    #    (format spécifique SIPLEC ou autre — dépend de la facture réelle)
    # 6. Renvoyer Facture(date_livraison, fournisseur, prix_achat_ht, reference)
    return None


def _charger_fixture_facture(chemin: str | Path | None) -> Facture | None:
    if chemin is None:
        return None
    chemin = Path(chemin)
    if not chemin.is_file():
        return None
    with chemin.open(encoding="utf-8") as f:
        data = json.load(f)
    prix = data.get("prix_achat_ht", {})
    return Facture(
        date_livraison=date.fromisoformat(data["date_livraison"]),
        fournisseur=data.get("fournisseur", "mock"),
        prix_achat_ht=PrixCarburants(
            gazole=prix.get("Gazole"),
            sp95_e10=prix.get("SP95-E10"),
            sp98=prix.get("SP98"),
            e85=prix.get("E85"),
        ),
        reference=data.get("reference", "mock-ref"),
    )


def parser_corps_facture_siplec(corps: str) -> PrixCarburants:
    """Parse le corps texte d'une facture SIPLEC pour extraire les prix HT.

    Format attendu (exemple, à affiner sur facture réelle) :
        SP95-E10 : 1,379 €/L HT
        SP98    : 1,454 €/L HT
        Gazole  : 1,371 €/L HT
    """
    patterns = {
        "E85": r"E85\s*:\s*([0-9],[0-9]{3})",
        "SP95-E10": r"SP95-E10\s*:\s*([0-9],[0-9]{3})",
        "SP98": r"SP98\s*:\s*([0-9],[0-9]{3})",
        "Gazole": r"Gazole\s*:\s*([0-9],[0-9]{3})",
    }
    prix = {}
    for carburant, pat in patterns.items():
        m = re.search(pat, corps, re.IGNORECASE)
        if m:
            prix[carburant] = float(m.group(1).replace(",", "."))

    return PrixCarburants(
        gazole=prix.get("Gazole"),
        sp95_e10=prix.get("SP95-E10"),
        sp98=prix.get("SP98"),
        e85=prix.get("E85"),
    )
