"""
config_loader
=============

Charge les 4 fichiers YAML de config, les valide, et détecte les placeholders
`A_COMPLETER` / `A_COMPLETER_PRISE_FONCTION` / `A_COMPLETER_VERIFIER_TERRAIN`.

Règle : en mode production (DRY_RUN=false), la présence de tout placeholder
bloque le démarrage. En DRY_RUN, les placeholders sont listés en log mais
n'empêchent pas l'exécution — c'est le but du mode dry-run.

Usage :
    from src.config_loader import load_all
    configs, placeholders = load_all(Path("../config"))
    if not placeholders:
        # Prod prête
        ...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Tous les tokens marqués "à compléter" reconnus
PLACEHOLDER_TOKENS = (
    "A_COMPLETER",
    "A_COMPLETER_PRISE_FONCTION",
    "A_COMPLETER_VERIFIER_TERRAIN",
)

# Regex : chaîne contenant "A_COMPLETER" (avec ou sans suffixe) ou "XXXXXXX"
PLACEHOLDER_REGEX = re.compile(r"(A_COMPLETER(?:_[A-Z_]+)?|X{4,})")

CONFIG_FILES = (
    "stations_config.yaml",
    "parametres_runs.yaml",
    "parametres_gmail.yaml",
    "parametres_magasin.yaml",
)


@dataclass(frozen=True)
class PlaceholderHit:
    """Un placeholder détecté dans la config : fichier, chemin YAML, valeur."""

    fichier: str
    chemin: str  # ex "ma_station.id_prix_carburants"
    valeur: str

    def __str__(self) -> str:
        return f"{self.fichier}: {self.chemin} = {self.valeur!r}"


def load_all(config_dir: Path | str) -> tuple[dict[str, dict], list[PlaceholderHit]]:
    """Charge les 4 YAML et renvoie (configs, placeholders).

    Args:
        config_dir: dossier contenant les 4 YAML.

    Returns:
        Tuple ``(configs, placeholders)`` où ``configs`` est un dict nom_fichier
        (sans extension) -> contenu YAML parsé, et ``placeholders`` la liste
        des :class:`PlaceholderHit` détectés tous fichiers confondus.

    Raises:
        FileNotFoundError: si un fichier de config attendu est manquant.
        yaml.YAMLError: si un YAML est mal formé.
    """
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        raise FileNotFoundError(f"Dossier config introuvable : {config_dir}")

    configs: dict[str, dict] = {}
    placeholders: list[PlaceholderHit] = []

    for fichier in CONFIG_FILES:
        chemin = config_dir / fichier
        if not chemin.is_file():
            raise FileNotFoundError(f"Config manquante : {chemin}")

        with chemin.open(encoding="utf-8") as f:
            contenu = yaml.safe_load(f)

        if contenu is None:
            raise ValueError(f"{fichier} est vide ou ne contient que des commentaires")

        cle = chemin.stem  # nom sans extension
        configs[cle] = contenu
        placeholders.extend(_scan_placeholders(contenu, fichier))

    log.info(
        "Configs chargées (%d fichiers), %d placeholders détectés",
        len(configs),
        len(placeholders),
    )
    return configs, placeholders


def _scan_placeholders(obj: Any, fichier: str, chemin: str = "") -> list[PlaceholderHit]:
    """Parcourt récursivement un objet YAML et liste les placeholders trouvés."""
    hits: list[PlaceholderHit] = []

    if isinstance(obj, dict):
        for cle, val in obj.items():
            nouveau_chemin = f"{chemin}.{cle}" if chemin else str(cle)
            hits.extend(_scan_placeholders(val, fichier, nouveau_chemin))
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            nouveau_chemin = f"{chemin}[{i}]"
            hits.extend(_scan_placeholders(val, fichier, nouveau_chemin))
    elif isinstance(obj, str):
        if PLACEHOLDER_REGEX.search(obj):
            hits.append(PlaceholderHit(fichier=fichier, chemin=chemin, valeur=obj))

    return hits


def _est_hard(p: PlaceholderHit) -> bool:
    """Un placeholder est 'hard' s'il bloque vraiment la prod.

    Les placeholders avec un suffixe explicite (_PRISE_FONCTION, _VERIFIER_TERRAIN)
    sont des marqueurs de "à compléter plus tard, à un moment connu" — ils
    n'empêchent pas le système de tourner aujourd'hui.

    Les placeholders bruts (`A_COMPLETER`, `XXXXXXX`) sont 'hard' car on ne sait
    pas quand ils seront remplis ni s'ils sont obligatoires.
    """
    val = p.valeur
    # Bruts = hard
    if val == "A_COMPLETER" or "XXXXXXX" in val or "XXXX" in val:
        return True
    # Avec suffixe = soft (différé volontaire)
    return False


def valider_pour_production(placeholders: list[PlaceholderHit]) -> None:
    """Lève RuntimeError si des placeholders HARD subsistent (mode prod).

    Les placeholders 'soft' (suffixés _PRISE_FONCTION, _VERIFIER_TERRAIN) sont
    tolérés en prod : ils représentent des valeurs à compléter à un moment connu
    futur (cession, vérification terrain) et n'empêchent pas le run de tourner.

    À appeler APRÈS load_all() quand DRY_RUN=false.
    """
    durs = [p for p in placeholders if _est_hard(p)]
    if durs:
        lignes = "\n  - ".join(str(p) for p in durs)
        raise RuntimeError(
            f"Impossible de démarrer en production : {len(durs)} placeholder(s) "
            f"BLOQUANT(S) non renseigné(s) :\n  - {lignes}\n\n"
            "Remplir ces valeurs ou relancer avec DRY_RUN=true."
        )


def resumer_placeholders(placeholders: list[PlaceholderHit]) -> str:
    """Rend un résumé lisible pour les logs (mode dry-run)."""
    if not placeholders:
        return "Aucun placeholder — configs prêtes pour la production."

    par_fichier: dict[str, list[PlaceholderHit]] = {}
    for p in placeholders:
        par_fichier.setdefault(p.fichier, []).append(p)

    lignes = [f"{len(placeholders)} placeholder(s) à compléter :"]
    for fichier, hits in par_fichier.items():
        lignes.append(f"  [{fichier}] ({len(hits)} entrée(s))")
        for h in hits:
            lignes.append(f"    - {h.chemin} = {h.valeur!r}")
    return "\n".join(lignes)
