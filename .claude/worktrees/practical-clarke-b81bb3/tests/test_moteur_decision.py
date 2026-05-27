"""
Tests unitaires du moteur de décision.

Cas couverts :
1. Calcul marge : conversion TTC / HT / cts/L
2. Plancher : OK et KO
3. Verrou intra-journée : actif et inactif depuis historique Sheet
4. Aucun mouvement → SILENCE
5. Concurrent baisse, alignement OK → ACTION
6. Concurrent baisse, alignement sous plancher → INFO
7. Nouvelle facture + marge hors cible → ACTION repricing
8. Verrou + concurrent sous plancher → INFO (exception)
9. Verrou seul → SILENCE
10. Alignement agressif marché (3+ baisses) → exception détectée
"""

from __future__ import annotations

from datetime import date

import pytest

from src.moteur_decision import (
    Action,
    PrixCarburants,
    calculer_marge,
    categoriser_concurrents,
    detecter_exceptions_verrou,
    prix_ttc_pour_marge_cible,
    proposer_repricing,
    verifier_plancher,
    verrou_intra_journee_actif,
)


# ------------------------------------------------------------
# 1. Calcul marge
# ------------------------------------------------------------


def test_calcul_marge_simple():
    # Gazole vendu 1.669 TTC (TVA 20%), acheté 1.30 HT
    # HT vente = 1.669 / 1.20 = 1.3908
    # Marge = 1.3908 - 1.30 = 0.0908 euros = 9.08 cts
    marge = calculer_marge(prix_vente_ttc=1.669, prix_achat_ht=1.30)
    assert round(marge.marge_cts_litre, 2) == 9.08
    assert round(marge.marge_pourcent, 4) == 0.0653  # 6.53%


def test_calcul_marge_negative():
    # Cas où on vend en dessous du prix d'achat (livraison chère, pas encore rétablie)
    marge = calculer_marge(prix_vente_ttc=1.50, prix_achat_ht=1.30)
    # HT vente = 1.25, marge = 1.25 - 1.30 = -0.05 euros = -5 cts
    assert marge.marge_cts_litre == pytest.approx(-5.0)


# ------------------------------------------------------------
# 2. Plancher
# ------------------------------------------------------------


def test_plancher_respecte():
    assert verifier_plancher(prix_propose_ttc=1.669, prix_achat_ht=1.30, plancher_cts=1.0) is True


def test_plancher_non_respecte():
    # Marge = ~-5 cts, plancher à 1.0 → refusé
    assert verifier_plancher(prix_propose_ttc=1.50, prix_achat_ht=1.30, plancher_cts=1.0) is False


def test_prix_ttc_pour_marge_cible():
    # Achat HT 1.30, cible 2 cts/L → vente HT = 1.32, vente TTC = 1.584
    prix = prix_ttc_pour_marge_cible(prix_achat_ht=1.30, marge_cible_cts=2.0)
    assert prix == pytest.approx(1.584, abs=0.001)


# ------------------------------------------------------------
# 3. Verrou intra-journée
# ------------------------------------------------------------


def test_verrou_actif_repricing_aujourdhui():
    historique = [
        {"Date": "2026-04-21", "Heure run": "07:00", "Statut": "statu quo"},
        {"Date": "2026-04-21", "Heure run": "10:00", "Statut": "repricing proposé"},
    ]
    assert verrou_intra_journee_actif(historique, date(2026, 4, 21)) is True


def test_verrou_inactif_statu_quo():
    historique = [
        {"Date": "2026-04-21", "Heure run": "07:00", "Statut": "statu quo"},
        {"Date": "2026-04-21", "Heure run": "10:00", "Statut": "statu quo"},
    ]
    assert verrou_intra_journee_actif(historique, date(2026, 4, 21)) is False


def test_verrou_inactif_autre_jour():
    historique = [{"Date": "2026-04-20", "Heure run": "10:00", "Statut": "appliqué"}]
    assert verrou_intra_journee_actif(historique, date(2026, 4, 21)) is False


def test_verrou_historique_vide():
    assert verrou_intra_journee_actif([], date(2026, 4, 21)) is False


# ------------------------------------------------------------
# 4. Catégorisation concurrents (config v2)
# ------------------------------------------------------------


def test_categoriser_concurrents_les_pieux():
    config = {
        "concurrent_principal": [
            {"nom": "Intermarché Les Pieux", "id_prix_carburants": "50340003"}
        ],
        "surveillance_rayon_15km": [
            {"nom": "Carrefour Market Bricquebec", "id_prix_carburants": "50260001"}
        ],
        "surveillance_hors_rayon": [
            {"nom": "Leclerc Querqueville", "id_prix_carburants": "50460001"}
        ],
    }
    cats = categoriser_concurrents(config)
    assert len(cats["principal"]) == 1
    assert cats["principal"][0]["nom"] == "Intermarché Les Pieux"
    assert len(cats["surveillance_rayon"]) == 1
    assert len(cats["surveillance_hors_rayon"]) == 1


# ------------------------------------------------------------
# 5-9. Scénarios de decision tree
# ------------------------------------------------------------


def _prix_stables():
    """Helper : nos prix TTC + prix achat HT stables."""
    nos = PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.669)
    achat = PrixCarburants(sp95_e10=1.28, sp98=1.35, gazole=1.30)
    return nos, achat


def test_scenario_aucun_mouvement_silence():
    nos, achat = _prix_stables()
    concurrent = {
        "Intermarché Les Pieux": PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.669)
    }
    result = proposer_repricing(nos, achat, concurrent)
    assert result.action == Action.SILENCE
    assert "statu quo" in result.justification.lower()


def test_scenario_concurrent_baisse_alignement_ok():
    nos, achat = _prix_stables()
    # Intermarché baisse le Gazole de 1 ct
    concurrent = {
        "Intermarché Les Pieux": PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.659)
    }
    result = proposer_repricing(nos, achat, concurrent)
    assert result.action == Action.ACTION
    assert result.carburant_cible == "Gazole"
    assert result.nouveaux_prix["Gazole"] == pytest.approx(1.659, abs=0.001)
    # Les autres carburants restent inchangés
    assert result.nouveaux_prix["SP98"] == pytest.approx(1.769, abs=0.001)


def test_scenario_concurrent_baisse_sous_plancher_info():
    nos, achat = _prix_stables()
    # Achat Gazole élevé : 1.65 HT. Nos prix TTC 1.669 = marge ~-0.09 cts
    # Concurrent baisse à 1.64 → alignement passerait sous plancher
    achat_cher = PrixCarburants(sp95_e10=1.28, sp98=1.35, gazole=1.65)
    concurrent = {
        "Intermarché Les Pieux": PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.64)
    }
    result = proposer_repricing(nos, achat_cher, concurrent, plancher_cts=1.0)
    assert result.action == Action.INFO
    assert "plancher" in result.justification.lower()


def test_scenario_nouvelle_facture_repricing_vers_cible():
    # Baseline : tous les carburants à marge 2 cts/L (=cible). Seul Gazole aura
    # une nouvelle facture qui décale sa marge.
    # Pour marge = 2 cts/L à TVA 20 % : prix_achat = prix_vente_ht - 0.02
    # Ex. Gazole vente 1.669 TTC → vente HT 1.3908 → achat HT 1.3708
    nos = PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.669)
    achat_stable = PrixCarburants(sp95_e10=1.3792, sp98=1.4542, gazole=1.3708)
    # Nouvelle livraison Gazole moins chère : achat passe de 1.3708 à 1.20
    achat_nouveau = PrixCarburants(
        sp95_e10=1.3792, sp98=1.4542, gazole=1.20  # Gazole : -17 cts HT
    )
    concurrent = {
        "Intermarché Les Pieux": PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.669)
    }
    result = proposer_repricing(
        nos,
        achat_nouveau,
        concurrent,
        nouvelle_facture=True,
        cible_cts=2.0,
    )
    # Baseline cohérente : autres carburants pile à cible, pas de déclenchement sur eux
    # Gazole = hors cible → déclenche repricing
    assert result.action == Action.ACTION
    assert result.carburant_cible == "Gazole"
    # Repricing Gazole vers cible 2 cts : achat 1.20 + 0.02 = 1.22 HT → 1.464 TTC
    assert result.nouveaux_prix["Gazole"] == pytest.approx(1.464, abs=0.002)
    # Sanity : baseline à cible juste (vérifie que la fixture est correcte)
    marge_baseline = calculer_marge(1.679, 1.3792).marge_cts_litre
    assert marge_baseline == pytest.approx(2.0, abs=0.3)


def test_scenario_verrou_actif_silence():
    nos, achat = _prix_stables()
    concurrent = {
        "Intermarché Les Pieux": PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.659)
    }
    # Verrou actif → même si concurrent baisse, pas d'action
    result = proposer_repricing(nos, achat, concurrent, verrou_actif=True)
    # Pas d'exception déclenchée → SILENCE
    assert result.action == Action.SILENCE


def test_scenario_verrou_avec_exception_info():
    # Verrou + concurrent passe sous plancher → INFO
    nos = PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.669)
    # Prix d'achat élevés → concurrent facile à passer sous plancher
    achat = PrixCarburants(sp95_e10=1.60, sp98=1.70, gazole=1.62)
    concurrent = {
        # Intermarché à 1.62 TTC, achat HT 1.62 → marge négative, sous plancher de 1 ct
        "Intermarché Les Pieux": PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.62)
    }
    result = proposer_repricing(nos, achat, concurrent, verrou_actif=True, plancher_cts=1.0)
    assert result.action == Action.INFO
    assert "concurrent_sous_plancher" in result.exceptions


def test_detection_alignement_agressif_marche():
    # 3+ baisses simultanées sur les stations de surveillance
    mouvements = [
        {"station": "Leclerc Querqueville", "delta_cts": -1.5, "carburant": "Gazole"},
        {"station": "Carrefour Market Bricquebec", "delta_cts": -1.2, "carburant": "Gazole"},
        {"station": "Station Flamanville village", "delta_cts": -0.8, "carburant": "E85"},
    ]
    nos = PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.669)
    achat = PrixCarburants(sp95_e10=1.28, sp98=1.35, gazole=1.30)
    concurrent = {
        "Intermarché Les Pieux": PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.669)
    }
    exceptions = detecter_exceptions_verrou(
        nos, achat, concurrent, mouvements_surveillance=mouvements
    )
    assert "alignement_agressif_marche" in exceptions


def test_simuler_marge_ponderee_calcul():
    """Test de base : marge pondérée par mix."""
    from src.moteur_decision import simuler_marge_ponderee
    nos = PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.669)
    achat = PrixCarburants(sp95_e10=1.3792, sp98=1.4542, gazole=1.3708)
    mix = {"SP95-E10": 0.50, "SP98": 0.10, "Gazole": 0.40}  # SP95 absent = 0
    pond = simuler_marge_ponderee(nos, achat, mix)
    # Toutes les marges sont environ 1.43% → pondération = ~1.43%
    assert 0.013 < pond < 0.016


# ------------------------------------------------------------
# Cascade 4 niveaux (cible pondérée)
# ------------------------------------------------------------


def _baseline_cascade():
    """Setup de référence pour les tests cascade : marge pondérée actuelle ~3,8%."""
    # vente HT moyenne ~1.45 €/L, achat ~1.40 → marge ~5 cts → ratio ~3,5%
    nos = PrixCarburants(sp95_e10=1.749, sp98=1.769, gazole=1.749)
    achat = PrixCarburants(sp95_e10=1.40, sp98=1.42, gazole=1.40)
    mix = {"SP95-E10": 0.50, "SP98": 0.10, "Gazole": 0.40}
    return nos, achat, mix


def test_cascade_niveau_1_cible_ponderee_preservee():
    """Concurrent baisse 1 ct, marge pondérée reste au-dessus de 3,5% → ACTION niveau 1."""
    nos, achat, mix = _baseline_cascade()
    concurrent = {"Intermarché": PrixCarburants(sp95_e10=1.749, sp98=1.769, gazole=1.739)}  # gazole -1 ct
    result = proposer_repricing(
        nos, achat, concurrent,
        cible_cts=2.0, plancher_cts=1.0,
        cible_ponderee_pct=0.035, mix=mix,
    )
    assert result.action == Action.ACTION
    assert result.niveau_cascade == 1


def test_cascade_niveau_2_decroche_pondere_garde_cts():
    """Concurrent baisse, pondérée passe sous 3,5% MAIS marge cts/L Gazole reste ≥ 2 cts → ACTION niveau 2.

    Achat Gazole 1.40, vente cible 2 cts = 1.704 TTC.
    Concurrent à 1.71 → alignement = marge 2,5 cts ≥ cible 2 cts/L (OK)
    mais pondérée simulée ~3,05% < 3,5% → niveau 2.
    """
    nos, achat, mix = _baseline_cascade()
    concurrent = {"Intermarché": PrixCarburants(sp95_e10=1.749, sp98=1.769, gazole=1.71)}
    result = proposer_repricing(
        nos, achat, concurrent,
        cible_cts=2.0, plancher_cts=1.0,
        cible_ponderee_pct=0.035, mix=mix,
    )
    assert result.action == Action.ACTION
    assert result.niveau_cascade == 2


def test_cascade_niveau_3_sous_cible_cts_garde_plancher():
    """Concurrent baisse plus fort. Marge cts/L < cible 2 mais ≥ plancher 1 → INFO niveau 3.

    Achat 1.40, plancher 1 ct = 1.692, cible 2 = 1.704.
    Concurrent à 1.694 → marge 1,17 cts (entre plancher et cible) → niveau 3.
    """
    nos, achat, mix = _baseline_cascade()
    concurrent = {"Intermarché": PrixCarburants(sp95_e10=1.749, sp98=1.769, gazole=1.694)}
    result = proposer_repricing(
        nos, achat, concurrent,
        cible_cts=2.0, plancher_cts=1.0,
        cible_ponderee_pct=0.035, mix=mix,
    )
    assert result.action == Action.INFO
    assert result.niveau_cascade == 3


def test_tolerance_info_si_ecart_inferieur_seuil():
    """Si écart concurrent ≤ seuil tolérance, INFO (pas d'ACTION mais signal)."""
    nos = PrixCarburants(sp95_e10=1.990, sp98=1.769, gazole=1.669)
    achat = PrixCarburants(sp95_e10=1.40, sp98=1.42, gazole=1.30)
    # Concurrent à 1.989, écart = 0.001 = exactement la tolérance
    concurrent = {"Intermarché": PrixCarburants(sp95_e10=1.989, sp98=1.769, gazole=1.669)}
    result = proposer_repricing(
        nos, achat, concurrent,
        seuil_tolerance_eur=0.001,
    )
    # Écart = 0.001 = tolérance → pas d'alignement mais INFO pour Benjamin
    assert result.action == Action.INFO
    assert "tolérance" in result.justification.lower() or "tolérés" in result.justification.lower()


def test_tolerance_alignement_si_ecart_strictement_superieur():
    """Si écart concurrent > seuil tolérance, alignement proposé."""
    nos = PrixCarburants(sp95_e10=1.990, sp98=1.769, gazole=1.669)
    achat = PrixCarburants(sp95_e10=1.40, sp98=1.42, gazole=1.30)
    # Concurrent à 1.988, écart = 0.002 > 0.001 → alignement
    concurrent = {"Intermarché": PrixCarburants(sp95_e10=1.988, sp98=1.769, gazole=1.669)}
    result = proposer_repricing(
        nos, achat, concurrent,
        seuil_tolerance_eur=0.001,
    )
    assert result.action == Action.ACTION
    assert result.nouveaux_prix["SP95-E10"] == pytest.approx(1.988, abs=0.0001)


def test_multi_carburants_alignement():
    """Si 2 carburants en retard, les 2 sont alignés en une seule reco."""
    # Mes prix : E10 1.99, SP98 1.769, Gazole 2.15
    nos = PrixCarburants(sp95_e10=1.99, sp98=1.769, gazole=2.15)
    achat = PrixCarburants(sp95_e10=1.40, sp98=1.42, gazole=1.40)
    # Concurrent baisse E10 et Gazole
    concurrent = {"Intermarché": PrixCarburants(sp95_e10=1.97, sp98=1.769, gazole=2.10)}
    result = proposer_repricing(
        nos, achat, concurrent,
        seuil_tolerance_eur=0.001,
    )
    assert result.action == Action.ACTION
    # Les 2 carburants doivent apparaître dans nouveaux_prix
    assert result.nouveaux_prix["SP95-E10"] == pytest.approx(1.97, abs=0.001)
    assert result.nouveaux_prix["Gazole"] == pytest.approx(2.10, abs=0.001)
    # SP98 (pas en retard) reste à notre prix
    assert result.nouveaux_prix["SP98"] == pytest.approx(1.769, abs=0.001)
    # carburant_cible inclut les 2
    assert "SP95-E10" in result.carburant_cible
    assert "Gazole" in result.carburant_cible


def test_cascade_niveau_4_sous_plancher():
    """Concurrent passe sous plancher → INFO URGENTE niveau 4."""
    nos, achat, mix = _baseline_cascade()
    # achat 1.40, plancher 1 ct → vente plancher = 1.692. Concurrent à 1.65 → < plancher.
    concurrent = {"Intermarché": PrixCarburants(sp95_e10=1.749, sp98=1.769, gazole=1.65)}
    result = proposer_repricing(
        nos, achat, concurrent,
        cible_cts=2.0, plancher_cts=1.0,
        cible_ponderee_pct=0.035, mix=mix,
    )
    assert result.action == Action.INFO
    assert result.niveau_cascade == 4
    assert "URGENTE" in result.justification or "plancher" in result.justification.lower()


def test_detection_autre_super_u_signal():
    # Super U Beaumont-Hague bouge 2 cts sur 2 carburants
    mouvements = [
        {"station": "Super U Beaumont-Hague", "delta_cts": -2.5, "carburant": "Gazole"},
        {"station": "Super U Beaumont-Hague", "delta_cts": 2.0, "carburant": "SP98"},
    ]
    nos = PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.669)
    achat = PrixCarburants(sp95_e10=1.28, sp98=1.35, gazole=1.30)
    concurrent = {
        "Intermarché Les Pieux": PrixCarburants(sp95_e10=1.679, sp98=1.769, gazole=1.669)
    }
    exceptions = detecter_exceptions_verrou(
        nos, achat, concurrent, mouvements_surveillance=mouvements
    )
    assert "autre_super_u_signal" in exceptions
