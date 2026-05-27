"""
visuel_concurrence
==================

Génère le PNG du relevé concurrence à envoyer en push Telegram.

Tableau coloré : nos prix vs concurrents, écart en cts/L, dernier
changement de prix relevé par l'API gouv, marge pondérée vs cible.

Format PNG en mémoire (bytes) → envoi via Telegram sendPhoto.

Carburants affichés dans l'ordre par importance volume Les Pieux :
Gazole > SP95-E10 > SP98 > E85.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# Ordre par importance volume Les Pieux (cohérent avec moteur_decision.CARBURANTS)
CARBURANTS_AFFICHAGE = ["Gazole", "SP95-E10", "SP98", "E85"]


def generer_png_concurrence(
    prix_concurrents: dict[str, dict],
    nos_prix: dict[str, float | None],
    nom_nous: str,
    marge_ponderee: float | None,
    marge_cible: float | None,
    maintenant: datetime,
    alignements_partiels: dict[str, list[str]] | None = None,
    prix_appliques: dict[str, float | None] | None = None,
) -> bytes:
    """Génère le PNG du relevé concurrence.

    Args:
        prix_concurrents : {nom_station: {"Gazole": float, "SP95-E10": float, ...,
                            "derniere_maj_api": "07/05 12:08"}} pour chaque concurrent.
        nos_prix : nos prix de vente TTC actuels {Gazole, SP95-E10, SP98, E85}.
        nom_nous : ex "Super U Les Pieux".
        marge_ponderee : ratio (ex 0.0353 pour 3,53%) ou None.
        marge_cible : ratio (ex 0.035 pour 3,50%) ou None.
        maintenant : datetime du run.
        alignements_partiels : {nom_station: [carburants alignés]}. Pour Bricquebec
                               typiquement {"Super U Bricquebec": ["Gazole", "SP95-E10"]}.
                               Les diffs vs nous ne sont affichées QUE pour ces carburants.

    Returns:
        bytes : PNG du visuel, prêt à envoyer via Telegram sendPhoto.
    """
    # Import lazy : matplotlib lent à charger, pas la peine si pas utilisé
    import matplotlib
    matplotlib.use("Agg")  # backend non-interactif (pour serveur)
    import matplotlib.pyplot as plt

    align = alignements_partiels or {}

    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.set_axis_off()

    # Titre
    date_run = maintenant.strftime("%d/%m/%Y %Hh%M")
    fig.suptitle(
        f"RELEVÉ CONCURRENCE — {date_run}",
        fontsize=18, fontweight="bold", y=0.97, color="#1a237e",
    )

    # Construction du tableau
    colonnes = ["Station"] + CARBURANTS_AFFICHAGE + ["Dernier chgmt"]
    n_cols = len(colonnes)

    cell_text = []
    cell_colors = []

    # Ligne 1 : nous (en valeur, mise en bleu)
    label_nous = f"{nom_nous} (nous, AVANT)" if prix_appliques else f"{nom_nous} (nous)"
    ligne_nous = [label_nous]
    couleur_nous = ["#e3f2fd"]
    for c in CARBURANTS_AFFICHAGE:
        ligne_nous.append(_fmt_prix(nos_prix.get(c)))
        couleur_nous.append("#bbdefb")
    ligne_nous.append("-")
    couleur_nous.append("#e3f2fd")
    cell_text.append(ligne_nous)
    cell_colors.append(couleur_nous)

    # Si on vient d'appliquer de nouveaux prix : ligne supplementaire
    # "NOUVEAUX prix appliques" en vert pour bien differencier l'avant/apres.
    if prix_appliques:
        ligne_new = [f"{nom_nous} (NOUVEAUX prix)"]
        couleur_new = ["#c8e6c9"]
        for c in CARBURANTS_AFFICHAGE:
            nv = prix_appliques.get(c) if prix_appliques.get(c) is not None else nos_prix.get(c)
            ligne_new.append(_fmt_prix(nv))
            couleur_new.append("#a5d6a7")
        ligne_new.append("OK")
        couleur_new.append("#c8e6c9")
        cell_text.append(ligne_new)
        cell_colors.append(couleur_new)

    # Lignes concurrents (1 ligne prix + 1 ligne diff vs nous)
    for nom, data in prix_concurrents.items():
        # Ligne prix
        ligne = [nom]
        couleur_ligne = ["white"]
        for c in CARBURANTS_AFFICHAGE:
            ligne.append(_fmt_prix(data.get(c)))
            couleur_ligne.append("white")
        ligne.append(str(data.get("derniere_maj_api", "")))
        couleur_ligne.append("white")
        cell_text.append(ligne)
        cell_colors.append(couleur_ligne)

        # Ligne diff (uniquement carburants alignés si alignement partiel défini)
        carbs_align = align.get(nom, CARBURANTS_AFFICHAGE)
        ligne_diff = ["  vs nous (cts/L)"]
        couleur_diff = ["#fafafa"]
        for c in CARBURANTS_AFFICHAGE:
            if c not in carbs_align:
                ligne_diff.append("")
                couleur_diff.append("#fafafa")
                continue
            txt, col = _fmt_diff_cts(nos_prix.get(c), data.get(c))
            ligne_diff.append(txt)
            couleur_diff.append(col)
        ligne_diff.append("")
        couleur_diff.append("#fafafa")
        cell_text.append(ligne_diff)
        cell_colors.append(couleur_diff)

    table = ax.table(
        cellText=cell_text,
        colLabels=colonnes,
        cellColours=cell_colors,
        colColours=["#1a237e"] * n_cols,
        cellLoc="center",
        loc="center",
        colWidths=[0.32, 0.10, 0.13, 0.10, 0.12, 0.16],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(11)
    table.scale(1, 1.8)

    for c in range(n_cols):
        cell = table[0, c]
        cell.set_text_props(color="white", fontweight="bold")
        cell.set_height(0.07)

    for r in range(len(cell_text) + 1):
        if r == 0:
            continue
        cell = table[r, 0]
        cell.set_text_props(ha="left")
        cell.PAD = 0.02

    # Footer marge pondérée vs cible
    if marge_ponderee is not None and marge_cible is not None:
        delta_pts = (marge_ponderee - marge_cible) * 100  # ratio -> pts
        signe = "+" if delta_pts >= 0 else ""
        couleur_marge = "#2e7d32" if delta_pts >= 0 else "#c62828"
        icon = "OK" if delta_pts >= 0 else "ALERTE"
        # Si photo post-application : libellé "APRÈS" pour clarifier
        label_marge = "Marge pondérée APRÈS" if prix_appliques else "Marge pondérée actuelle"
        footer = (
            f"{label_marge} : {marge_ponderee * 100:.2f}% "
            f"| Cible : {marge_cible * 100:.2f}% "
            f"| Delta : {signe}{delta_pts:.2f} pts ({icon})"
        ).replace(".", ",")
        fig.text(0.5, 0.05, footer, ha="center", fontsize=12,
                 fontweight="bold", color=couleur_marge,
                 bbox=dict(boxstyle="round,pad=0.5", facecolor="#fff",
                           edgecolor=couleur_marge, linewidth=1.5))

    # Légende couleurs
    legende_y = 0.13
    legende_elems = [
        ("vert : moins chers que concurrent", "#c8e6c9"),
        ("rouge : plus chers", "#ffcdd2"),
        ("jaune : écart faible (<0,5c)", "#fff9c4"),
        ("bleu : nos prix", "#bbdefb"),
    ]
    x_start = 0.05
    for i, (txt, col) in enumerate(legende_elems):
        x = x_start + i * 0.23
        fig.patches.append(plt.Rectangle((x, legende_y), 0.015, 0.018,
                                         transform=fig.transFigure,
                                         facecolor=col, edgecolor="#888",
                                         linewidth=0.5))
        fig.text(x + 0.018, legende_y + 0.005, txt, fontsize=9, color="#555")

    plt.subplots_adjust(top=0.88, bottom=0.18)

    # Génération PNG en mémoire
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _fmt_prix(p) -> str:
    if p is None or p == "":
        return "—"
    try:
        return f"{float(p):.3f}".replace(".", ",")
    except (ValueError, TypeError):
        return "—"


def _fmt_diff_cts(p_nous, p_concu) -> tuple[str, str]:
    """Renvoie (txt, couleur) pour l'écart nous-concu en cts/L."""
    if p_nous is None or p_concu is None:
        return "", "white"
    try:
        diff_cts = (float(p_nous) - float(p_concu)) * 100
    except (ValueError, TypeError):
        return "", "white"
    signe = "+" if diff_cts >= 0 else ""
    txt = f"{signe}{diff_cts:.1f}c".replace(".", ",")
    if diff_cts < -0.5:
        couleur = "#c8e6c9"  # vert : on bat la concu
    elif diff_cts > 0.5:
        couleur = "#ffcdd2"  # rouge : on est plus cher
    else:
        couleur = "#fff9c4"  # jaune : écart faible
    return txt, couleur
