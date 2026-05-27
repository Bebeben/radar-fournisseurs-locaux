"""
Tests mail_builder — format français, bloc à déclarer, mails ACTION / INFO / SILENCE.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.mail_builder import (
    Mail,
    bloc_a_declarer,
    construire_mail,
    construire_sujet,
    format_marge_cts,
    format_prix_euros,
    format_marge_pourcent,
)
from src.moteur_decision import Action, MargeInfo, Proposition


# ------------------------------------------------------------
# Formatage français
# ------------------------------------------------------------


def test_format_prix_virgule():
    assert format_prix_euros(1.669) == "1,669 €"


def test_format_prix_none():
    assert format_prix_euros(None) == "—"


def test_format_marge_cts_signe():
    assert format_marge_cts(2.15) == "+2,15 cts/L"
    assert format_marge_cts(-0.80) == "-0,80 cts/L"


def test_format_marge_pourcent_pourcent():
    assert format_marge_pourcent(0.0142) == "1,42 %"


# ------------------------------------------------------------
# Bloc à déclarer
# ------------------------------------------------------------


def test_bloc_a_declarer_signale_nouveau():
    prix_nouveaux = {"SP95-E10": 1.679, "SP98": 1.769, "Gazole": 1.659}
    prix_anciens = {"SP95-E10": 1.679, "SP98": 1.769, "Gazole": 1.669}
    bloc = bloc_a_declarer(prix_nouveaux, prix_anciens)
    # Gazole a changé → NOUVEAU
    assert "Gazole" in bloc and "NOUVEAU" in bloc
    # Les autres sont inchangés
    assert "(inchangé)" in bloc
    # URL prix-carburants.gouv.fr présente
    assert "prix-carburants.gouv.fr" in bloc
    # Virgule française respectée
    assert "1,659" in bloc


# ------------------------------------------------------------
# Sujet
# ------------------------------------------------------------


def _config_gmail_minimal():
    return {
        "destinataire": {"email_principal": "benjamin@lespieux.fr"},
        "expediteur": {"email": "pricing@lespieux.fr", "nom_affiche": "Pricing Les Pieux"},
        "sujets": {
            "prefixe_action": "[Pricing carburant] Proposition de repricing",
            "prefixe_info": "[INFO — pas d'action aujourd'hui]",
        },
    }


def test_sujet_action():
    prop = Proposition(action=Action.ACTION, justification="test")
    sujet = construire_sujet(prop, _config_gmail_minimal(), datetime(2026, 4, 21, 10, 30))
    assert sujet.startswith("[Pricing carburant] Proposition de repricing")
    assert "21/04 10h" in sujet


def test_sujet_info_avec_exception():
    prop = Proposition(
        action=Action.INFO, justification="test", exceptions=("concurrent_sous_plancher",)
    )
    sujet = construire_sujet(prop, _config_gmail_minimal(), datetime(2026, 4, 21, 7, 0))
    assert "INFO" in sujet
    assert "Concurrent sous plancher" in sujet


# ------------------------------------------------------------
# Mail complet
# ------------------------------------------------------------


def test_construire_mail_silence_renvoie_none():
    prop = Proposition(action=Action.SILENCE, justification="rien")
    mail = construire_mail(prop, {}, _config_gmail_minimal(), datetime(2026, 4, 21, 10, 0), dry_run=True)
    assert mail is None


def test_construire_mail_action_structure_complete():
    marges = {
        "Gazole": MargeInfo(
            prix_vente_ttc=1.659,
            prix_vente_ht=1.3825,
            prix_achat_ht=1.36,
            marge_cts_litre=2.25,
            marge_pourcent=0.0163,
        ),
    }
    prop = Proposition(
        action=Action.ACTION,
        justification="Intermarché baisse Gazole à 1,659 €",
        nouveaux_prix={"SP95-E10": 1.679, "SP98": 1.769, "Gazole": 1.659},
        marges=marges,
        carburant_cible="Gazole",
    )
    prix_actuels = {"SP95-E10": 1.679, "SP98": 1.769, "Gazole": 1.669}
    mail = construire_mail(
        prop,
        prix_actuels,
        _config_gmail_minimal(),
        datetime(2026, 4, 21, 10, 30),
        dry_run=True,
        sheet_url="https://docs.google.com/sheet/xyz",
    )
    assert isinstance(mail, Mail)
    assert mail.destinataire == "benjamin@lespieux.fr"
    # Corps contient les éléments clés
    assert "Bonjour Benjamin" in mail.corps
    assert "10h30" in mail.corps
    assert "À déclarer sur prix-carburants.gouv.fr" in mail.corps
    assert "(NOUVEAU)" in mail.corps  # Gazole a changé
    assert "(inchangé)" in mail.corps  # SP98, SP95-E10 inchangés
    assert "1,659 €" in mail.corps  # format français
    assert "DRY_RUN" in mail.corps
    assert "docs.google.com" in mail.corps


def test_construire_mail_info_structure():
    prop = Proposition(
        action=Action.INFO,
        justification="Concurrent sous plancher détecté",
        exceptions=("concurrent_sous_plancher",),
    )
    prix_actuels = {"SP95-E10": 1.679, "SP98": 1.769, "Gazole": 1.669}
    mail = construire_mail(
        prop,
        prix_actuels,
        _config_gmail_minimal(),
        datetime(2026, 4, 21, 10, 0),
        dry_run=False,
    )
    assert mail is not None
    assert "INFO" in mail.sujet
    # Corps INFO n'a pas de bloc "À déclarer"
    assert "À déclarer" not in mail.corps
    # Mais a le signal d'exception
    assert "concurrent sous plancher" in mail.corps
    # Mode PRODUCTION signalé
    assert "PRODUCTION" in mail.corps
