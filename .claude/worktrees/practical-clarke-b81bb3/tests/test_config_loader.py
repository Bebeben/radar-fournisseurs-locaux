"""
Tests config_loader — chargement YAML, détection placeholders, validation prod.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config_loader import (
    CONFIG_FILES,
    PlaceholderHit,
    load_all,
    resumer_placeholders,
    valider_pour_production,
)

# Dossier config — interne au projet (./config), avec fallback sur ../config (legacy).
_INTERNAL = Path(__file__).resolve().parent.parent / "config"
_LEGACY = Path(__file__).resolve().parent.parent.parent / "config"
CONFIG_REEL = _INTERNAL if _INTERNAL.is_dir() else _LEGACY


def test_load_all_charge_les_4_fichiers():
    configs, _ = load_all(CONFIG_REEL)
    # Les 4 fichiers attendus doivent être chargés
    expected = {Path(f).stem for f in CONFIG_FILES}
    assert set(configs.keys()) == expected


def test_load_all_stations_config_structure():
    configs, _ = load_all(CONFIG_REEL)
    stations = configs["stations_config"]
    # Notre station est bien au 2 route de Flamanville après correction
    assert stations["ma_station"]["id_prix_carburants"] == "50340002"
    # Concurrent principal unique = Intermarché Les Pieux
    assert len(stations["concurrent_principal"]) == 1
    assert stations["concurrent_principal"][0]["enseigne"] == "Intermarché"


def test_placeholders_detectes_prise_fonction():
    _, placeholders = load_all(CONFIG_REEL)
    # Au moins quelques placeholders de type PRISE_FONCTION doivent être trouvés
    # (SIRET, emails, login gérant, etc. qu'on remplira après cession)
    tokens_trouves = {p.valeur for p in placeholders}
    has_prise_fonction = any("PRISE_FONCTION" in t for t in tokens_trouves)
    assert has_prise_fonction, f"Aucun A_COMPLETER_PRISE_FONCTION trouvé : {tokens_trouves}"


def test_resumer_placeholders_lisible():
    _, placeholders = load_all(CONFIG_REEL)
    resume = resumer_placeholders(placeholders)
    assert "placeholder" in resume.lower()
    # Le résumé doit au minimum mentionner un des fichiers
    assert any(Path(f).name in resume for f in CONFIG_FILES)


def test_valider_pour_production_tolere_soft_placeholders():
    """Les placeholders 'soft' (PRISE_FONCTION, VERIFIER_TERRAIN) ne bloquent pas.

    Notre config Les Pieux n'en a que des soft → la validation passe.
    """
    _, placeholders = load_all(CONFIG_REEL)
    valider_pour_production(placeholders)  # ne doit rien lever


def test_valider_pour_production_rejette_hard_placeholder():
    """Un placeholder brut A_COMPLETER (sans suffixe) bloque la prod."""
    from src.config_loader import PlaceholderHit
    hits = [PlaceholderHit(fichier="x.yaml", chemin="a.b", valeur="A_COMPLETER")]
    with pytest.raises(RuntimeError, match="BLOQUANT"):
        valider_pour_production(hits)


def test_valider_pour_production_rejette_xxxxx():
    from src.config_loader import PlaceholderHit
    hits = [PlaceholderHit(fichier="x.yaml", chemin="id", valeur="XXXXXXX")]
    with pytest.raises(RuntimeError, match="BLOQUANT"):
        valider_pour_production(hits)


def test_valider_pour_production_accepte_liste_vide():
    valider_pour_production([])  # ne doit rien lever


def test_load_all_levee_si_dossier_manquant(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_all(tmp_path / "inexistant")


def test_placeholder_hit_affichage():
    hit = PlaceholderHit(
        fichier="parametres_magasin.yaml",
        chemin="identite.siret",
        valeur="A_COMPLETER_PRISE_FONCTION",
    )
    s = str(hit)
    assert "parametres_magasin.yaml" in s
    assert "identite.siret" in s
    assert "A_COMPLETER_PRISE_FONCTION" in s
