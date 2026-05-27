"""
mail_builder
============

Compose les mails ACTION / INFO à partir d'une :class:`Proposition` produite
par :mod:`moteur_decision`. Fonctions pures — pas d'envoi réel, juste
construction de sujets et de corps. L'envoi lui-même est dans :mod:`main`.

Format strict défini dans `docs/proposition_automation.md` §5 et respecté ici :
- virgule française pour les prix affichés (1,669 € pas 1.669 €)
- bloc "À déclarer sur prix-carburants.gouv.fr" obligatoire dans toute ACTION
- signature avec indication DRY_RUN / PRODUCTION
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.moteur_decision import (
    CARBURANTS,
    Action,
    MargeInfo,
    Proposition,
)

URL_ESPACE_GERANT = "https://www.prix-carburants.gouv.fr/oilstations/login"


@dataclass(frozen=True)
class Mail:
    """Représentation d'un mail prêt à être envoyé."""

    destinataire: str
    copie: str | None
    expediteur: str
    expediteur_nom: str
    sujet: str
    corps: str


# ============================================================
# Helpers de formatage
# ============================================================


def format_prix_euros(prix: float | None) -> str:
    """Format français : 1,669 € (virgule, 3 décimales)."""
    if prix is None:
        return "—"
    return f"{prix:.3f} €".replace(".", ",")


def format_marge_cts(cts: float) -> str:
    """Format signé avec 2 décimales : +2,15 cts/L ou -0,80 cts/L."""
    signe = "+" if cts >= 0 else ""
    return f"{signe}{cts:.2f} cts/L".replace(".", ",")


def format_marge_pourcent(taux: float) -> str:
    """Format en pourcentage : 1,42 %."""
    return f"{taux * 100:.2f} %".replace(".", ",")


# ============================================================
# Construction du bloc "À déclarer"
# ============================================================


def bloc_a_declarer(
    prix_ttc: dict[str, float],
    prix_precedents: dict[str, float | None],
) -> str:
    """Construit le bloc de déclaration pour prix-carburants.gouv.fr.

    Args:
        prix_ttc: prix finaux (après repricing) par carburant.
        prix_precedents: prix d'avant, pour signaler (inchangé) vs (NOUVEAU).
    """
    lignes = ["À déclarer sur prix-carburants.gouv.fr (espace gérant) :"]
    for c in CARBURANTS:
        nouveau = prix_ttc.get(c)
        ancien = prix_precedents.get(c)
        if nouveau is None:
            continue
        if ancien is not None and abs(nouveau - ancien) < 0.001:
            tag = "(inchangé)"
        else:
            tag = "(NOUVEAU)"
        # Espaces pour aligner proprement même avec SP95-E10 (8 car max pour nom carburant)
        lignes.append(f"  • {c:<9} : {format_prix_euros(nouveau)} {tag}")
    lignes.append("")
    lignes.append(f"Lien direct : {URL_ESPACE_GERANT}")
    return "\n".join(lignes)


# ============================================================
# Construction sujet
# ============================================================


def _horodatage_sujet(maintenant: datetime) -> str:
    """Format '21/04 10h' pour le sujet."""
    return maintenant.strftime("%d/%m %Hh")


def construire_sujet(
    proposition: Proposition,
    config_gmail: dict,
    maintenant: datetime,
) -> str:
    sujets = config_gmail.get("sujets", {})
    horodatage = _horodatage_sujet(maintenant)
    if proposition.action == Action.ACTION:
        prefixe = sujets.get("prefixe_action", "[Pricing carburant] Proposition de repricing")
        return f"{prefixe} — {horodatage}"
    if proposition.action == Action.INFO:
        prefixe = sujets.get("prefixe_info", "[INFO — pas d'action aujourd'hui]")
        # Ajouter un contexte court si exception connue
        contexte = _contexte_mail_info(proposition)
        if contexte:
            return f"{prefixe} {contexte} — {horodatage}"
        return f"{prefixe} — {horodatage}"
    # SILENCE : jamais appelé normalement, mais fournir un fallback
    return f"[Silencieux — aucun mail] — {horodatage}"


def _contexte_mail_info(proposition: Proposition) -> str:
    if "concurrent_sous_plancher" in proposition.exceptions:
        return "Concurrent sous plancher"
    if "alignement_agressif_marche" in proposition.exceptions:
        return "Alignement agressif marché"
    if "autre_super_u_signal" in proposition.exceptions:
        return "Signal réseau U"
    if proposition.carburant_cible:
        return f"Info {proposition.carburant_cible}"
    return ""


# ============================================================
# Construction corps du mail
# ============================================================


def construire_corps(
    proposition: Proposition,
    nos_prix_actuels: dict[str, float | None],
    maintenant: datetime,
    dry_run: bool,
    sheet_url: str | None = None,
    config_gmail: dict | None = None,
) -> str:
    """Assemble le corps du mail selon l'action."""
    config_gmail = config_gmail or {}
    if proposition.action == Action.ACTION:
        return _corps_action(proposition, nos_prix_actuels, maintenant, dry_run, sheet_url, config_gmail)
    if proposition.action == Action.INFO:
        return _corps_info(proposition, nos_prix_actuels, maintenant, dry_run, sheet_url, config_gmail)
    # SILENCE : pas de corps
    return ""


def _corps_action(
    proposition: Proposition,
    nos_prix_actuels: dict[str, float | None],
    maintenant: datetime,
    dry_run: bool,
    sheet_url: str | None,
    config_gmail: dict,
) -> str:
    heure = maintenant.strftime("%Hh%M")
    carburant = proposition.carburant_cible or "—"

    sections: list[str] = [
        "Bonjour Benjamin,",
        "",
        f"Mouvement détecté à {heure} :",
        f"- {proposition.justification}",
        "",
        "Marges résultantes (après repricing proposé) :",
    ]

    # Détail des marges par carburant
    for c in CARBURANTS:
        marge = proposition.marges.get(c)
        if marge is None:
            continue
        ligne = (
            f"  • {c:<9} : vente {format_prix_euros(proposition.nouveaux_prix.get(c, marge.prix_vente_ttc))} "
            f"| achat {format_prix_euros(marge.prix_achat_ht)} HT "
            f"| marge {format_marge_cts(marge.marge_cts_litre)} "
            f"| taux {format_marge_pourcent(marge.marge_pourcent)}"
        )
        sections.append(ligne)
    sections.append("")

    # Bloc "À déclarer"
    sections.append(bloc_a_declarer(proposition.nouveaux_prix, nos_prix_actuels))
    sections.append("")

    if sheet_url:
        sections.append(f"Historique complet : {sheet_url}")
        sections.append("")

    sections.append(_signature(config_gmail, dry_run))
    _ = carburant  # usage éventuel futur
    return "\n".join(sections)


def _corps_info(
    proposition: Proposition,
    nos_prix_actuels: dict[str, float | None],
    maintenant: datetime,
    dry_run: bool,
    sheet_url: str | None,
    config_gmail: dict,
) -> str:
    heure = maintenant.strftime("%Hh%M")
    sections: list[str] = [
        "Bonjour Benjamin,",
        "",
        f"Signal détecté à {heure} (pas d'action demandée aujourd'hui) :",
        f"- {proposition.justification}",
        "",
    ]

    if proposition.exceptions:
        sections.append("Exceptions relevées :")
        for exc in proposition.exceptions:
            sections.append(f"  - {exc.replace('_', ' ')}")
        sections.append("")

    # Prix actuels pour contexte
    sections.append("Prix affichés actuels :")
    for c in CARBURANTS:
        p = nos_prix_actuels.get(c)
        if p is not None:
            sections.append(f"  • {c:<9} : {format_prix_euros(p)}")
    sections.append("")

    if sheet_url:
        sections.append(f"Historique complet : {sheet_url}")
        sections.append("")

    sections.append(_signature(config_gmail, dry_run))
    return "\n".join(sections)


def _signature(config_gmail: dict, dry_run: bool) -> str:
    template = config_gmail.get("format", {}).get("signature")
    if template:
        base = template.strip()
    else:
        base = (
            "---\n"
            "Mail généré automatiquement par routine Claude Code\n"
            "Cadence : 3 runs/jour (7h, 10h, 13h) du lundi au samedi"
        )
    mode = "DRY_RUN (simulation, aucun effet réel)" if dry_run else "PRODUCTION"
    return f"{base}\nMode : {mode}"


# ============================================================
# Assemblage final
# ============================================================


def construire_mail(
    proposition: Proposition,
    nos_prix_actuels: dict[str, float | None],
    config_gmail: dict,
    maintenant: datetime,
    dry_run: bool,
    sheet_url: str | None = None,
) -> Mail | None:
    """Construit un :class:`Mail` prêt à être envoyé, ou None si SILENCE."""
    if proposition.action == Action.SILENCE:
        return None

    sujet = construire_sujet(proposition, config_gmail, maintenant)
    corps = construire_corps(
        proposition,
        nos_prix_actuels,
        maintenant,
        dry_run,
        sheet_url,
        config_gmail,
    )

    dest = config_gmail.get("destinataire", {}).get("email_principal", "")
    copie = config_gmail.get("destinataire", {}).get("email_copie_optionnel") or None
    exp = config_gmail.get("expediteur", {}).get("email", "")
    exp_nom = config_gmail.get("expediteur", {}).get("nom_affiche", "Pricing carburant Les Pieux")

    return Mail(
        destinataire=dest,
        copie=copie,
        expediteur=exp,
        expediteur_nom=exp_nom,
        sujet=sujet,
        corps=corps,
    )
