"""
moteur_decision
===============

Cœur métier : applique la stratégie tarifaire "cible + plancher" pour
proposer un repricing. Toutes les fonctions sont **pures** (pas d'I/O,
pas de logging de niveau WARNING+) — elles sont donc totalement testables
avec des fixtures.

La logique globale suit l'arbre de décision du doc `docs/strategie_tarifaire.md`
et le tableau des flux mail de `docs/proposition_automation.md`.

Notes de vocabulaire (cohérentes avec docs/) :
- "concurrent_principal"  : liste d'UN ou plusieurs concurrents avec alignement actif
                            (dans notre config Les Pieux : uniquement Intermarché Les Pieux)
- "surveillance_rayon"    : monitoring seul, pas d'alignement auto
- "surveillance_hors_rayon" : marché élargi, signal info uniquement
- "cible"                 : marge brute visée en cts/L (défaut 2.0)
- "plancher"              : marge minimale en cts/L (défaut 1.0, adaptable)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Literal

# Carburants standardisés (ordre par importance volume Les Pieux : Gazole > E10 > SP98 > E85)
# SP95 pur n'est plus tracké (volume négligeable), remplacé par E85 (carburant économique).
CARBURANTS = ("Gazole", "SP95-E10", "SP98", "E85")
type Carburant = Literal["Gazole", "SP95-E10", "SP98", "E85"]

TVA_FR = 0.20
CIBLE_MARGE_CTS_DEFAUT = 2.0
PLANCHER_MARGE_CTS_DEFAUT = 1.0
SEUIL_ECART_POST_LIVRAISON_CTS = 0.3
SEUIL_ECART_STRUCTURANT_SECONDAIRE_CTS = 3.0
SEUIL_AUTRE_SUPER_U_CTS = 2.0
NB_BAISSES_ALIGNEMENT_AGRESSIF = 3
SEUIL_TOLERANCE_EUR_DEFAUT = 0.001  # 0.1 ct/L : on ne s'aligne pas si on est ≤ 0.001 € au-dessus


class Action(str, Enum):
    """Trois résultats possibles d'un run.

    Note vocab : la valeur "STATU QUO" est visible dans le Sheet et les notifs,
    le nom Python ``Action.SILENCE`` reste pour rétrocompat (signifie : pas de
    repricing demandé, prix actuels OK).
    """

    ACTION = "ACTION"  # Notif de proposition de repricing à envoyer
    INFO = "INFO"  # Notif informative (pas d'action demandée, décision manuelle)
    SILENCE = "STATU QUO"  # Aucun mouvement, prix actuels OK


@dataclass(frozen=True)
class PrixCarburants:
    """Prix d'un jeu de carburants à un instant T."""

    gazole: float | None = None
    sp95_e10: float | None = None
    sp98: float | None = None
    e85: float | None = None

    def get(self, carburant: Carburant) -> float | None:
        return {
            "Gazole": self.gazole,
            "SP95-E10": self.sp95_e10,
            "SP98": self.sp98,
            "E85": self.e85,
        }[carburant]

    def as_dict(self) -> dict[Carburant, float | None]:
        return {c: self.get(c) for c in CARBURANTS}  # type: ignore[dict-item]


@dataclass(frozen=True)
class MargeInfo:
    """Résultat d'un calcul de marge sur UN carburant."""

    prix_vente_ttc: float
    prix_vente_ht: float
    prix_achat_ht: float
    marge_cts_litre: float  # en centimes d'euro par litre
    marge_pourcent: float  # (prix_vente_ht - prix_achat_ht) / prix_vente_ht


@dataclass(frozen=True)
class Proposition:
    """Résultat final du moteur pour un run donné."""

    action: Action
    justification: str
    nouveaux_prix: dict[str, float] = field(default_factory=dict)  # TTC €/L
    marges: dict[str, MargeInfo] = field(default_factory=dict)
    exceptions: tuple[str, ...] = ()  # exceptions verrou détectées
    carburant_cible: str | None = None  # carburant sur lequel porte le repricing si ACTION
    niveau_cascade: int | None = None  # 1=cible pondérée OK, 2=dégradé cible cts/L, 3=sous cible cts, 4=sous plancher
    marge_ponderee_simulee: float | None = None  # ratio 0-1 (utile en log/notif)


# ============================================================
# Fonctions pures — calculs
# ============================================================


def calculer_marge(
    prix_vente_ttc: float,
    prix_achat_ht: float,
    tva_taux: float = TVA_FR,
) -> MargeInfo:
    """Calcule marge_cts/L et marge % pour un carburant.

    Args:
        prix_vente_ttc: prix affiché en caisse, €/L
        prix_achat_ht: prix d'achat hors taxes (hors TVA et hors TICPE côté achat),
            €/L. La TICPE est incluse côté vente TTC comme côté achat HT selon
            la norme métier retenue par Benjamin.
        tva_taux: défaut 20 % (France).

    Returns:
        :class:`MargeInfo` immuable.
    """
    prix_vente_ht = prix_vente_ttc / (1 + tva_taux)
    marge_euros = prix_vente_ht - prix_achat_ht
    marge_cts = marge_euros * 100
    marge_pourcent = (marge_euros / prix_vente_ht) if prix_vente_ht > 0 else 0.0
    return MargeInfo(
        prix_vente_ttc=prix_vente_ttc,
        prix_vente_ht=prix_vente_ht,
        prix_achat_ht=prix_achat_ht,
        marge_cts_litre=marge_cts,
        marge_pourcent=marge_pourcent,
    )


def verifier_plancher(
    prix_propose_ttc: float,
    prix_achat_ht: float,
    plancher_cts: float = PLANCHER_MARGE_CTS_DEFAUT,
    tva_taux: float = TVA_FR,
) -> bool:
    """True si le prix proposé respecte le plancher de marge.

    Le plancher est paramétrable pour gérer les cas :
    - opération prix coûtant nationale : plancher = 0.5 cts
    - tension marché (grève/pénurie) : plancher = 1.5 cts
    """
    marge = calculer_marge(prix_propose_ttc, prix_achat_ht, tva_taux)
    return marge.marge_cts_litre >= plancher_cts


def simuler_marge_ponderee(
    nos_prix_ttc: PrixCarburants,
    prix_achat_ht: PrixCarburants,
    mix: dict[str, float],
    tva_taux: float = TVA_FR,
) -> float:
    """Calcule la marge brute % pondérée par le mix de vente.

    Args:
        nos_prix_ttc: prix de vente affichés
        prix_achat_ht: prix d'achat HT
        mix: dict carburant → ratio 0-1 (somme idéale 1.0)
        tva_taux: défaut 20 %

    Returns:
        Ratio 0-1 (ex 0.0432 pour 4,32%). 0 si données insuffisantes.

    Méthode :
        marge_pondérée = Σ(mix_i × marge_HT_i) / Σ(mix_i × vente_HT_i)
    """
    sum_marge = 0.0
    sum_vente_ht = 0.0
    for c in CARBURANTS:
        prix_ttc = nos_prix_ttc.get(c)  # type: ignore[arg-type]
        achat = prix_achat_ht.get(c)  # type: ignore[arg-type]
        weight = mix.get(c, 0.0)
        if prix_ttc and achat and weight > 0:
            vente_ht = prix_ttc / (1 + tva_taux)
            marge_ht = vente_ht - achat
            sum_marge += weight * marge_ht
            sum_vente_ht += weight * vente_ht
    return sum_marge / sum_vente_ht if sum_vente_ht > 0 else 0.0


def prix_ttc_pour_marge_cible(
    prix_achat_ht: float,
    marge_cible_cts: float = CIBLE_MARGE_CTS_DEFAUT,
    tva_taux: float = TVA_FR,
) -> float:
    """Inverse de calculer_marge : quel prix TTC affiche-t-on pour atteindre X cts/L ?"""
    marge_euros = marge_cible_cts / 100
    prix_vente_ht = prix_achat_ht + marge_euros
    return round(prix_vente_ht * (1 + tva_taux), 3)


# ============================================================
# Règles temporelles
# ============================================================


def verrou_intra_journee_actif(
    historique: list[dict],
    date_aujourdhui: date,
) -> bool:
    """True si un repricing a déjà été appliqué aujourd'hui.

    Args:
        historique: lignes de l'onglet "Historique" du Sheet, ordonnées
            chronologiquement. Chaque ligne est un dict avec au minimum
            les clés ``"Date"`` (YYYY-MM-DD) et ``"Statut"``.
        date_aujourdhui: date du run en cours (tz Europe/Paris).

    Returns:
        True si une ligne porte la date du jour avec un statut indiquant
        qu'un changement a été appliqué ("appliqué", "repricing proposé", ...).
    """
    jour = date_aujourdhui.isoformat()
    statuts_modificatifs = {"appliqué", "repricing proposé", "prix modifié"}
    for ligne in historique:
        if ligne.get("Date") == jour:
            statut = str(ligne.get("Statut", "")).strip().lower()
            if any(s in statut for s in statuts_modificatifs):
                return True
    return False


def est_jour_actif(jour_semaine_iso: int, weekend_actif: bool = False) -> bool:
    """Retourne True si on doit tourner (lun-sam standard, dim jamais).

    Args:
        jour_semaine_iso: 1 = lundi, 7 = dimanche (datetime.isoweekday()).
        weekend_actif: si True, dimanche inclus (désactivé par défaut,
            conforme à strategie_tarifaire §4).
    """
    if jour_semaine_iso == 7:  # dimanche
        return weekend_actif
    return True


# ============================================================
# Catégorisation concurrents
# ============================================================


def categoriser_concurrents(stations_config: dict) -> dict[str, list[dict]]:
    """Renvoie les concurrents rangés par catégorie métier.

    La config Les Pieux (v2) utilise une structure simplifiée :
    un concurrent principal unique + surveillance.

    Returns:
        Dict avec clés :
        - ``principal``     : alignement actif
        - ``surveillance_rayon`` : dans 15 km, monitoring
        - ``surveillance_hors_rayon`` : hors rayon, signal marché
    """
    return {
        "principal": list(stations_config.get("concurrent_principal", [])),
        "surveillance_rayon": list(stations_config.get("surveillance_rayon_15km", [])),
        "surveillance_hors_rayon": list(stations_config.get("surveillance_hors_rayon", [])),
    }


# ============================================================
# Détection exceptions verrou
# ============================================================


def detecter_exceptions_verrou(
    nos_prix: PrixCarburants,
    prix_achat_ht: PrixCarburants,
    prix_concurrent_principal: dict[str, PrixCarburants],  # nom_station -> prix
    mouvements_surveillance: list[dict],  # [{"station": str, "delta_cts": float, "carburant": str}, ...]
    plancher_cts: float = PLANCHER_MARGE_CTS_DEFAUT,
) -> tuple[str, ...]:
    """Détecte les 3 exceptions qui déclenchent un mail INFO même sous verrou.

    Exceptions couvertes (cf. docs/strategie_tarifaire.md §4.2) :
    1. ``concurrent_sous_plancher``      : un concurrent principal passe sous notre plancher
    2. ``alignement_agressif_marche``    : 3+ stations surveillance baissent le même jour
    3. ``autre_super_u_signal``          : autre Super U bouge >= 2 cts sur >= 2 carburants
    """
    exceptions: list[str] = []

    # 1. Concurrent principal sous plancher
    for _, prix in prix_concurrent_principal.items():
        for carburant in CARBURANTS:
            p_concurrent = prix.get(carburant)  # type: ignore[arg-type]
            p_achat = prix_achat_ht.get(carburant)  # type: ignore[arg-type]
            if p_concurrent is None or p_achat is None:
                continue
            marge = calculer_marge(p_concurrent, p_achat)
            if marge.marge_cts_litre < plancher_cts:
                exceptions.append("concurrent_sous_plancher")
                break
        if "concurrent_sous_plancher" in exceptions:
            break

    # 2. Alignement agressif marché : 3+ baisses simultanées sur structurants
    nb_baisses = sum(1 for m in mouvements_surveillance if m.get("delta_cts", 0) < 0)
    if nb_baisses >= NB_BAISSES_ALIGNEMENT_AGRESSIF:
        exceptions.append("alignement_agressif_marche")

    # 3. Autre Super U bouge significativement
    super_u_mouvements: dict[str, list[float]] = {}
    for m in mouvements_surveillance:
        nom = str(m.get("station", ""))
        if "Super U" in nom or "Hyper U" in nom:
            super_u_mouvements.setdefault(nom, []).append(abs(m.get("delta_cts", 0)))
    for nom, deltas in super_u_mouvements.items():
        mouvements_significatifs = [d for d in deltas if d >= SEUIL_AUTRE_SUPER_U_CTS]
        if len(mouvements_significatifs) >= 2:
            exceptions.append("autre_super_u_signal")
            break

    return tuple(dict.fromkeys(exceptions))  # dédup en gardant l'ordre


# ============================================================
# Moteur principal
# ============================================================


def proposer_repricing(
    nos_prix_ttc: PrixCarburants,
    prix_achat_ht: PrixCarburants,
    concurrents_principaux: dict[str, PrixCarburants],  # nom -> prix
    *,
    verrou_actif: bool = False,
    nouvelle_facture: bool = False,
    mouvements_surveillance: list[dict] | None = None,
    cible_cts: float = CIBLE_MARGE_CTS_DEFAUT,
    plancher_cts: float = PLANCHER_MARGE_CTS_DEFAUT,
    cible_ponderee_pct: float | None = None,  # ratio 0-1, None = pas de cascade
    mix: dict[str, float] | None = None,
    tva_taux: float = TVA_FR,
    seuil_tolerance_eur: float = SEUIL_TOLERANCE_EUR_DEFAUT,
    concurrents_carburants: dict[str, list[str]] | None = None,
) -> Proposition:
    """Applique l'arbre de décision complet et renvoie une proposition.

    Arbre (cf. docs/strategie_tarifaire.md §5) :

    1. Verrou actif ?
       - Oui → veille silencieuse, uniquement exceptions
       - Non → continuer
    2. Nouvelle facture ?
       - Oui + écart marge actuelle / cible > 0.3 cts → ACTION repricing vers cible
    3. Concurrent principal < nous sur un carburant ?
       - Alignement possible (reste >= plancher) → ACTION alignement
       - Alignement sous plancher → INFO alerte manuelle
    4. Rien ne bouge → SILENCE
    """
    mouvements_surveillance = mouvements_surveillance or []

    # Marges actuelles (utiles dans tous les cas pour le mail)
    marges_actuelles = _calculer_marges_toutes(nos_prix_ttc, prix_achat_ht)

    # Exceptions verrou (s'appliquent même si verrou OFF : elles renforcent une proposition)
    exceptions = detecter_exceptions_verrou(
        nos_prix_ttc,
        prix_achat_ht,
        concurrents_principaux,
        mouvements_surveillance,
        plancher_cts=plancher_cts,
    )

    # ÉTAPE 1 — Verrou actif
    if verrou_actif:
        if exceptions:
            return Proposition(
                action=Action.INFO,
                justification=(
                    "Verrou intra-journée actif. Exceptions détectées : "
                    + ", ".join(exceptions)
                    + ". Mail d'info pour brief lendemain."
                ),
                marges=marges_actuelles,
                exceptions=exceptions,
            )
        return Proposition(
            action=Action.SILENCE,
            justification="Verrou intra-journée actif, aucune exception — veille silencieuse.",
            marges=marges_actuelles,
        )

    # ÉTAPE 2 — Nouvelle facture : repricing vers cible si écart significatif
    if nouvelle_facture:
        carburant_off, proposition_facture = _proposition_post_livraison(
            nos_prix_ttc, prix_achat_ht, cible_cts, plancher_cts
        )
        if proposition_facture is not None:
            return Proposition(
                action=Action.ACTION,
                justification=(
                    f"Nouvelle facture : marge {carburant_off} hors cible de plus de "
                    f"{SEUIL_ECART_POST_LIVRAISON_CTS} cts. Repricing vers cible {cible_cts} cts/L."
                ),
                nouveaux_prix=proposition_facture,
                marges=marges_actuelles,
                carburant_cible=carburant_off,
                exceptions=exceptions,
            )

    # ÉTAPE 3 — Détection écarts concurrents (alignement réel + écarts tolérables)
    ecarts = _proposer_alignements_concurrents(
        nos_prix_ttc, prix_achat_ht, concurrents_principaux,
        plancher_cts, seuil_tolerance_eur,
        concurrents_carburants=concurrents_carburants,
    )
    if ecarts:
        # Séparer les alignements à appliquer (action_required) et les écarts tolérables (info only)
        a_aligner = [e for e in ecarts if e[4]]  # action_required = True
        toleres = [e for e in ecarts if not e[4]]  # action_required = False

        # Construire le dict de prix simulés (= nos prix + alignements appliqués)
        # Pour les tolérés, on N'aligne PAS, donc le prix reste inchangé.
        prix_simules_dict = dict(nos_prix_ttc.as_dict())
        for carb, prix_aligne, _, _, _ in a_aligner:
            prix_simules_dict[carb] = prix_aligne

        # Marge pondérée simulée GLOBALE (après alignements ACTION uniquement)
        marge_pond_simulee: float | None = None
        if mix and cible_ponderee_pct is not None:
            sim = PrixCarburants(
                gazole=prix_simules_dict.get("Gazole"),
                sp95_e10=prix_simules_dict.get("SP95-E10"),
                sp98=prix_simules_dict.get("SP98"),
                e85=prix_simules_dict.get("E85"),
            )
            marge_pond_simulee = simuler_marge_ponderee(sim, prix_achat_ht, mix, tva_taux)

        # Pour chaque alignement à appliquer, calculer son niveau individuel.
        details_action: list[dict] = []
        niveau_global = 1
        for carb, prix_aligne, sous_plancher, concurrent_nom, _ in a_aligner:
            achat_carb = prix_achat_ht.get(carb)  # type: ignore[arg-type]
            marge_carb_apres = (
                calculer_marge(prix_aligne, achat_carb, tva_taux).marge_cts_litre
                if achat_carb is not None else None
            )
            if sous_plancher:
                n = 4
            elif marge_pond_simulee is not None and cible_ponderee_pct is not None and marge_pond_simulee >= cible_ponderee_pct:
                n = 1
            elif marge_carb_apres is not None and marge_carb_apres >= cible_cts:
                n = 2
            else:
                n = 3
            niveau_global = max(niveau_global, n)
            details_action.append({
                "carburant": carb, "prix_aligne": prix_aligne, "concurrent": concurrent_nom,
                "marge_cts": marge_carb_apres, "niveau": n,
            })

        # Détails des écarts tolérés (info seulement, pas d'alignement)
        details_toleres: list[dict] = []
        for carb, prix_concurrent, _, concurrent_nom, _ in toleres:
            notre_prix = nos_prix_ttc.get(carb)  # type: ignore[arg-type]
            ecart_cts = (notre_prix - prix_concurrent) * 100 if notre_prix else 0
            details_toleres.append({
                "carburant": carb, "concurrent": concurrent_nom,
                "prix_concurrent": prix_concurrent, "ecart_cts": ecart_cts,
            })

        nouveaux_prix_dict = {k: v for k, v in prix_simules_dict.items() if v is not None}
        pond_str = f"{marge_pond_simulee * 100:.2f}%".replace(".", ",") if marge_pond_simulee is not None else "n/a"
        cible_pond_str = f"{cible_ponderee_pct * 100:.2f}%".replace(".", ",") if cible_ponderee_pct is not None else "n/a"

        # Construction du texte de justification
        parts = []
        if details_action:
            txt_action = "; ".join(
                f"{d['carburant']} → {d['prix_aligne']:.3f} € ({d['concurrent']}, "
                f"marge {d['marge_cts']:.2f} cts/L, niveau {d['niveau']})".replace(".", ",")
                for d in details_action
            )
            parts.append(f"À aligner ({len(details_action)}) : {txt_action}")
        if details_toleres:
            txt_tol = "; ".join(
                f"{d['carburant']} : {d['concurrent']} à {d['prix_concurrent']:.3f} € "
                f"(écart {d['ecart_cts']:.1f} cts ≤ tolérance)".replace(".", ",")
                for d in details_toleres
            )
            parts.append(f"Tolérés ({len(details_toleres)}) : {txt_tol}")
        marge_str = (
            f" — marge pondérée simulée {pond_str} (cible {cible_pond_str})"
            if cible_ponderee_pct is not None else ""
        )

        # Cas 1 : que des écarts tolérés (rien à aligner) → INFO simple
        if not details_action:
            justif = (
                "Concurrent légèrement sous nos prix mais dans la tolérance. "
                + parts[0] if parts else "Aucun mouvement"
            )
            return Proposition(
                action=Action.INFO,
                justification=justif,
                marges=marges_actuelles,
                carburant_cible=", ".join(d["carburant"] for d in details_toleres),
                exceptions=exceptions,
                niveau_cascade=None,
                marge_ponderee_simulee=marge_pond_simulee,
            )

        # Cas 2 : au moins un alignement à appliquer → ACTION ou INFO selon niveau cascade
        carburant_cible_str = ", ".join(d["carburant"] for d in details_action)
        justif = " | ".join(parts) + marge_str

        if niveau_global == 1:
            return Proposition(
                action=Action.ACTION,
                justification=justif + (" ✓" if cible_ponderee_pct else ""),
                nouveaux_prix=nouveaux_prix_dict,
                marges=marges_actuelles,
                carburant_cible=carburant_cible_str,
                exceptions=exceptions,
                niveau_cascade=1,
                marge_ponderee_simulee=marge_pond_simulee,
            )
        if niveau_global == 2:
            return Proposition(
                action=Action.ACTION,
                justification=justif + " ⚠ pondérée décroche, marge cts/L OK",
                nouveaux_prix=nouveaux_prix_dict,
                marges=marges_actuelles,
                carburant_cible=carburant_cible_str,
                exceptions=exceptions,
                niveau_cascade=2,
                marge_ponderee_simulee=marge_pond_simulee,
            )
        if niveau_global == 3:
            return Proposition(
                action=Action.INFO,
                justification=justif + " — décision manuelle",
                marges=marges_actuelles,
                carburant_cible=carburant_cible_str,
                exceptions=exceptions,
                niveau_cascade=3,
                marge_ponderee_simulee=marge_pond_simulee,
            )
        # niveau_global == 4 : sous plancher
        return Proposition(
            action=Action.INFO,
            justification=justif + " — au moins un sous plancher → décision URGENTE",
            marges=marges_actuelles,
            exceptions=exceptions,
            carburant_cible=carburant_cible_str,
            niveau_cascade=4,
            marge_ponderee_simulee=marge_pond_simulee,
        )

    # ÉTAPE 4 — Exceptions sans mouvement concurrent = INFO isolé (rare)
    if exceptions:
        return Proposition(
            action=Action.INFO,
            justification=(
                "Aucun mouvement concurrent principal mais exceptions détectées : "
                + ", ".join(exceptions)
            ),
            marges=marges_actuelles,
            exceptions=exceptions,
        )

    # ÉTAPE 5 — Statu quo
    return Proposition(
        action=Action.SILENCE,
        justification="Aucun mouvement concurrent, marges en cible, statu quo.",
        marges=marges_actuelles,
    )


# ============================================================
# Helpers internes
# ============================================================


def _calculer_marges_toutes(
    nos_prix_ttc: PrixCarburants,
    prix_achat_ht: PrixCarburants,
) -> dict[str, MargeInfo]:
    """Calcule la marge de chaque carburant présent des deux côtés."""
    out: dict[str, MargeInfo] = {}
    for c in CARBURANTS:
        p_vente = nos_prix_ttc.get(c)  # type: ignore[arg-type]
        p_achat = prix_achat_ht.get(c)  # type: ignore[arg-type]
        if p_vente is not None and p_achat is not None:
            out[c] = calculer_marge(p_vente, p_achat)
    return out


def _proposition_post_livraison(
    nos_prix_ttc: PrixCarburants,
    prix_achat_ht: PrixCarburants,
    cible_cts: float,
    plancher_cts: float,
) -> tuple[str, dict[str, float] | None]:
    """Cherche le premier carburant dont la marge s'écarte de la cible de > seuil.

    Si trouvé et que le repricing vers cible respecte le plancher, renvoie
    le nouveau jeu de prix. Sinon (None).
    """
    for c in CARBURANTS:
        p_vente = nos_prix_ttc.get(c)  # type: ignore[arg-type]
        p_achat = prix_achat_ht.get(c)  # type: ignore[arg-type]
        if p_vente is None or p_achat is None:
            continue
        marge = calculer_marge(p_vente, p_achat)
        ecart = abs(marge.marge_cts_litre - cible_cts)
        if ecart > SEUIL_ECART_POST_LIVRAISON_CTS:
            nouveau = prix_ttc_pour_marge_cible(p_achat, cible_cts)
            # Vérifier plancher (cas rare : cible elle-même < plancher après hausse achat)
            if verifier_plancher(nouveau, p_achat, plancher_cts):
                prix_proposes = dict(nos_prix_ttc.as_dict())
                prix_proposes[c] = nouveau
                return c, {k: v for k, v in prix_proposes.items() if v is not None}
            # Sinon : remonter au moins au plancher
            prix_plancher = prix_ttc_pour_marge_cible(p_achat, plancher_cts)
            prix_proposes = dict(nos_prix_ttc.as_dict())
            prix_proposes[c] = prix_plancher
            return c, {k: v for k, v in prix_proposes.items() if v is not None}
    return "", None


def _proposer_alignements_concurrents(
    nos_prix_ttc: PrixCarburants,
    prix_achat_ht: PrixCarburants,
    concurrents_principaux: dict[str, PrixCarburants],
    plancher_cts: float,
    seuil_tolerance_eur: float = SEUIL_TOLERANCE_EUR_DEFAUT,
    concurrents_carburants: dict[str, list[str]] | None = None,
) -> list[tuple[str, float, bool, str, bool]]:
    """Scanne TOUS les carburants et renvoie tous les écarts détectés.

    Args:
        concurrents_carburants: dict ``{nom: [carburants alignés]}`` — pour chaque concurrent,
            la liste des carburants sur lesquels on doit aligner. Si None, on aligne sur tous.
            Permet de gérer un concurrent en alignement PARTIEL (ex Bricquebec uniquement
            sur Gazole et E10).

    Returns:
        Liste de tuples ``(carburant, prix_concurrent_ttc, sous_plancher, nom_concurrent, action_required)``.
    """
    concurrents_carburants = concurrents_carburants or {}
    out: list[tuple[str, float, bool, str, bool]] = []
    for c in CARBURANTS:
        notre_prix = nos_prix_ttc.get(c)  # type: ignore[arg-type]
        prix_achat = prix_achat_ht.get(c)  # type: ignore[arg-type]
        if notre_prix is None or prix_achat is None:
            continue

        # Trouver le concurrent principal avec le prix le plus bas sur ce carburant
        # (uniquement parmi ceux qui sont alignés sur CE carburant)
        plus_bas_nom: str | None = None
        plus_bas_prix: float | None = None
        for nom, prix_c in concurrents_principaux.items():
            # Filtre par carburant : si le concurrent a une liste explicite, vérifier
            carbs_alignes = concurrents_carburants.get(nom)
            if carbs_alignes is not None and c not in carbs_alignes:
                continue  # ce concurrent ne compte pas pour ce carburant
            p = prix_c.get(c)  # type: ignore[arg-type]
            if p is None:
                continue
            if plus_bas_prix is None or p < plus_bas_prix:
                plus_bas_prix = p
                plus_bas_nom = nom

        if plus_bas_prix is None or plus_bas_nom is None:
            continue

        # Concurrent strictement sous nos prix = signal (au minimum INFO)
        ecart = notre_prix - plus_bas_prix
        if ecart > 0:
            sous_plancher = not verifier_plancher(plus_bas_prix, prix_achat, plancher_cts)
            action_required = ecart > seuil_tolerance_eur
            out.append((c, plus_bas_prix, sous_plancher, plus_bas_nom, action_required))

    return out
