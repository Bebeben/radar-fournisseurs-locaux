"""
recap_hebdo
===========

Génère un mail de récap hebdomadaire le lundi matin.

Contenu :
- Marge pondérée moyenne semaine N-1
- Nombre d'ACTION / INFO / SILENCE
- Mouvements clés des concurrents
- Tendance vs semaine N-2

Pas de proposition de repricing — uniquement du reporting.
"""

from __future__ import annotations

import logging
import statistics
from datetime import date, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)


def generer_recap_hebdo(
    historique_reco_prix: list[dict],
    historique_concurrents: list[dict],
    aujourd_hui: date,
) -> str:
    """Génère le corps du mail récap hebdomadaire.

    Args:
        historique_reco_prix: lignes de l'onglet Reco prix (dicts).
            Doivent contenir au minimum : Date, Heure, Statut, Marge pondérée %.
        historique_concurrents: lignes de l'onglet Concurrents (dicts).
            Doivent contenir : Date, Heure, Station, SP95-E10, SP98, Gazole.
        aujourd_hui: date du run (lundi typiquement).

    Returns:
        Corps du mail au format texte. Vide si pas assez de données.
    """
    debut_n = aujourd_hui - timedelta(days=7)  # lundi N-1
    debut_n_moins_1 = aujourd_hui - timedelta(days=14)  # lundi N-2

    reco_n = _filtrer_periode(historique_reco_prix, debut_n, aujourd_hui)
    reco_n_1 = _filtrer_periode(historique_reco_prix, debut_n_moins_1, debut_n)
    concu_n = _filtrer_periode(historique_concurrents, debut_n, aujourd_hui)

    if not reco_n:
        return ""  # pas assez de données

    # Stats semaine N-1
    statuts = [r.get("Statut", "").upper() for r in reco_n]
    nb_action = sum(1 for s in statuts if s == "ACTION")
    nb_info = sum(1 for s in statuts if s == "INFO")
    nb_silence = sum(1 for s in statuts if s in ("STATU QUO", "SILENCE"))  # rétrocompat

    marges_pond = [_to_float(r.get("Marge pondérée %")) for r in reco_n]
    marges_pond = [m for m in marges_pond if m is not None]
    marge_pond_moy_n = statistics.mean(marges_pond) if marges_pond else None

    marges_pond_n_1 = [_to_float(r.get("Marge pondérée %")) for r in reco_n_1]
    marges_pond_n_1 = [m for m in marges_pond_n_1 if m is not None]
    marge_pond_moy_n_1 = statistics.mean(marges_pond_n_1) if marges_pond_n_1 else None

    # Tendance
    delta = None
    if marge_pond_moy_n is not None and marge_pond_moy_n_1 is not None:
        delta = marge_pond_moy_n - marge_pond_moy_n_1

    # Mouvements concurrents : nb baisses détectées
    mouvements = _detecter_mouvements_concurrents(concu_n)

    # Construction du mail
    sections = [
        "Bonjour Benjamin,",
        "",
        f"Récap pricing carburants — semaine du {debut_n.strftime('%d/%m')} au {(aujourd_hui - timedelta(days=1)).strftime('%d/%m')}",
        "",
        "─── PERFORMANCE ───",
    ]

    if marge_pond_moy_n is not None:
        sections.append(f"Marge pondérée moyenne : {marge_pond_moy_n * 100:.2f}%".replace(".", ","))
    if delta is not None:
        signe = "+" if delta >= 0 else ""
        sections.append(f"Évolution vs semaine précédente : {signe}{delta * 100:.2f} pts".replace(".", ","))

    sections.extend([
        "",
        "─── ACTIVITÉ DU SYSTÈME ───",
        f"  • {nb_action} proposition(s) d'ACTION (alignement)",
        f"  • {nb_info} alerte(s) INFO (décision manuelle)",
        f"  • {nb_silence} run(s) SILENCE (statu quo)",
        f"  • Total : {len(reco_n)} runs",
        "",
    ])

    if mouvements:
        sections.append("─── MOUVEMENTS CONCURRENTS ───")
        for m in mouvements[:5]:  # top 5
            sections.append(f"  • {m}")
        sections.append("")

    sections.extend([
        "─── À FAIRE CETTE SEMAINE ───",
        "  • Vérifier que le Sheet est cohérent (Pricing live à jour si livraison)",
        "  • Si marge < cible : envisager hausse 0,5-1 ct sur le carburant le plus rentable",
        "  • Mettre à jour le mix de vente (onglet Paramètres) si dérive observée",
        "",
        "---",
        "Récap généré automatiquement le lundi 7h",
        "Système pricing carburants Super U Les Pieux",
    ])

    return "\n".join(sections)


def _filtrer_periode(rows: list[dict], debut: date, fin: date) -> list[dict]:
    """Garde les lignes dont la Date est dans [debut, fin)."""
    out = []
    for row in rows:
        date_str = str(row.get("Date", "")).strip()
        if not date_str:
            continue
        try:
            d = datetime.fromisoformat(date_str).date() if "T" in date_str else date.fromisoformat(date_str)
        except ValueError:
            continue
        if debut <= d < fin:
            out.append(row)
    return out


def _to_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("%", "").replace(",", ".").strip()
    try:
        f = float(s)
        return f / 100 if f > 1 else f  # détecte si c'est un % ou un ratio
    except ValueError:
        return None


def _detecter_mouvements_concurrents(rows: list[dict]) -> list[str]:
    """Détecte les mouvements significatifs (≥1 ct/L) dans la semaine.

    Pour chaque station + carburant, calcule le delta entre 1ère et dernière
    valeur observée dans la semaine.
    """
    par_station_carburant: dict[tuple, list[float]] = {}
    for row in rows:
        station = str(row.get("Station", ""))
        for carb in ("SP95-E10", "SP98", "Gazole"):
            v = _to_float(row.get(carb))
            if v is None or v > 5:  # filtre prix aberrants (>5€/L)
                continue
            par_station_carburant.setdefault((station, carb), []).append(v)

    mouvements = []
    for (station, carb), valeurs in par_station_carburant.items():
        if len(valeurs) < 2:
            continue
        delta = valeurs[-1] - valeurs[0]
        if abs(delta) >= 0.01:  # >= 1 ct/L
            signe = "+" if delta > 0 else ""
            mouvements.append(
                f"{station} {carb} : {signe}{delta * 100:.1f} cts ({valeurs[0]:.3f} → {valeurs[-1]:.3f} €)".replace(".", ",")
            )
    # Trier par amplitude décroissante
    mouvements.sort(key=lambda x: -abs(float(x.split(":")[1].split("cts")[0].strip().replace("+", "").replace(",", "."))))
    return mouvements
