"""Tests forçant le respect des leçons accumulées dans `tasks/lessons.md`.

Chaque test scanne le code source pour interdire le re-introduction d'un
bug déjà rencontré. Si Claude (ou quiconque) tente de re-faire l'erreur
le test fail et le commit est bloqué.

Plus solide qu'une règle "à se rappeler" — c'est exécutable.

Pour chaque test : référence l'entrée de `tasks/lessons.md` qui le motive.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


# ---------------------------------------------------------------------------
# LECON 2026-04-29 + 2026-05-08 : pas de filtre conditionnel skip silencieux
# ---------------------------------------------------------------------------


def test_no_run_heures_paris_filter():
    """RUN_HEURES_PARIS est mort. Tout pattern qui re-l'introduirait est interdit.

    Dette de la leçon du 29/04 (retirer le filtre quand on enlève le cron) +
    leçon du 08/05 (ne pas laisser un dead-code conditionnel piégeux).
    """
    fichiers_a_scanner = list(SRC.glob("*.py"))
    fichiers_a_scanner += list((ROOT / ".github" / "workflows").glob("*.yml"))

    coupables = []
    for f in fichiers_a_scanner:
        contenu = f.read_text(encoding="utf-8")
        # Retirer les lignes commentaires + docstrings (acceptés)
        lignes_actives = []
        for ligne in contenu.split("\n"):
            stripped = ligne.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            lignes_actives.append(ligne)
        contenu_actif = "\n".join(lignes_actives)
        if re.search(r"\bRUN_HEURES_PARIS\b", contenu_actif):
            coupables.append(str(f.relative_to(ROOT)))

    assert not coupables, (
        f"RUN_HEURES_PARIS retrouvé dans : {coupables}. "
        "Ce filtre doit rester mort (cf. lessons.md 2026-04-29 et 2026-05-08). "
        "Cron-job.org pilote l'heure, le code Python ne fait plus de filtre."
    )


# ---------------------------------------------------------------------------
# LECON 2026-05-01 : pas d'imports redondants dans fonctions (UnboundLocalError)
# ---------------------------------------------------------------------------


def test_no_redundant_imports_inside_functions():
    """Un symbole importé top-level ne doit JAMAIS être ré-importé dans une fonction.

    Sinon Python le traite comme variable locale dans toute la fonction → si
    un usage précède l'import dans le flow, UnboundLocalError silencieux.
    Cf. bug `from src.gsheet_io import GSheetIO` dans run_lecture_reponse.py
    qui a fait planter le workflow Lecture reponse 12h00 (lessons 2026-05-01).
    """
    coupables = []
    for f in SRC.glob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        # Collecter imports top-level
        top_level_names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    top_level_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top_level_names.add(alias.asname or alias.name.split(".")[0])

        # Pour chaque fonction, regarder les imports
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom):
                    for alias in sub.names:
                        nom = alias.asname or alias.name
                        if nom in top_level_names:
                            coupables.append(
                                f"{f.relative_to(ROOT)}::{node.name} ré-importe '{nom}' (déjà top-level)"
                            )
                elif isinstance(sub, ast.Import):
                    for alias in sub.names:
                        nom = alias.asname or alias.name.split(".")[0]
                        if nom in top_level_names:
                            coupables.append(
                                f"{f.relative_to(ROOT)}::{node.name} ré-importe '{nom}' (déjà top-level)"
                            )

    assert not coupables, (
        "Imports redondants détectés (risque UnboundLocalError) :\n  - "
        + "\n  - ".join(coupables)
        + "\nCf. lessons.md 2026-05-01."
    )


# ---------------------------------------------------------------------------
# LECON 2026-05-02 : datetime.fromtimestamp sans tz interdit
# ---------------------------------------------------------------------------


def test_no_naive_fromtimestamp():
    """`datetime.fromtimestamp(x)` sans `tz=` retourne un naive en heure locale.

    Sur GitHub Actions (UTC) vs Windows local (Paris), comportement différent.
    Source de bugs comparaisons aware/naive (cf. lessons 2026-05-02).
    Toujours utiliser `datetime.fromtimestamp(x, tz=ZoneInfo("UTC"))`.
    """
    pattern = re.compile(r"datetime\.fromtimestamp\(\s*[a-zA-Z_][a-zA-Z_0-9.]*\s*\)")
    coupables = []
    for f in SRC.glob("*.py"):
        contenu = f.read_text(encoding="utf-8")
        for n_ligne, ligne in enumerate(contenu.split("\n"), 1):
            stripped = ligne.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(ligne):
                coupables.append(f"{f.relative_to(ROOT)}:{n_ligne}: {stripped}")
    assert not coupables, (
        "datetime.fromtimestamp(...) sans tz= détecté :\n  - "
        + "\n  - ".join(coupables)
        + "\nUtilise `datetime.fromtimestamp(x, tz=ZoneInfo(\"UTC\"))`. Cf. lessons.md 2026-05-02."
    )


def test_no_replace_tzinfo_none_for_compare():
    """`.replace(tzinfo=None)` pour "homogénéiser" 2 datetimes est un piège.

    Si une est aware UTC et l'autre aware Paris, .replace(tzinfo=None) ne
    convertit PAS — il jette juste l'info tz et on compare des naives qui
    représentent des instants différents. Toujours convertir via astimezone.
    Cf. lessons 2026-05-02.
    """
    coupables = []
    for f in SRC.glob("*.py"):
        contenu = f.read_text(encoding="utf-8")
        for n_ligne, ligne in enumerate(contenu.split("\n"), 1):
            stripped = ligne.strip()
            if stripped.startswith("#"):
                continue
            if "replace(tzinfo=None)" in ligne:
                coupables.append(f"{f.relative_to(ROOT)}:{n_ligne}: {stripped}")
    assert not coupables, (
        ".replace(tzinfo=None) détecté (risque comparaison naive Paris vs UTC) :\n  - "
        + "\n  - ".join(coupables)
        + "\nUtilise .astimezone(target_tz) pour aligner deux datetimes. Cf. lessons.md 2026-05-02."
    )


# ---------------------------------------------------------------------------
# LECON 2026-05-01 : append_commande doit avoir un anti-doublon
# ---------------------------------------------------------------------------


def test_append_commande_has_doublon_check():
    """append_commande doit avoir un anti-doublon (cf. lessons 2026-05-01).

    Sinon un trigger répété insère plusieurs fois la même commande.
    """
    f = SRC / "gsheet_io.py"
    contenu = f.read_text(encoding="utf-8")

    # Extraire le corps de la fonction append_commande
    pattern = re.compile(r"def append_commande\([^)]*\)[^:]*:(.*?)(?=\n    def |\nclass |\Z)", re.DOTALL)
    match = pattern.search(contenu)
    assert match, "Fonction append_commande introuvable dans gsheet_io.py"
    corps = match.group(1)

    has_check = ("doublon" in corps.lower() or "_almost_equal" in corps or "anti-doublon" in corps.lower())
    assert has_check, (
        "append_commande n'a pas d'anti-doublon (cf. lessons.md 2026-05-01). "
        "Doit lire les commandes existantes et skip si même date + mêmes prix."
    )


# ---------------------------------------------------------------------------
# LECON 2026-05-08 : crash silencieux sur GitHub Actions = interdit
# ---------------------------------------------------------------------------


def test_main_has_telegram_crash_handler():
    """main.py doit notifier Telegram en cas de crash, pour ne pas avoir de
    silence sur GitHub Actions (cf. lessons 2026-05-08)."""
    contenu = (SRC / "main.py").read_text(encoding="utf-8")
    assert "_notifier_crash_telegram" in contenu, (
        "main.py n'a pas de handler de crash → silence garanti sur GitHub Actions. "
        "Cf. lessons.md 2026-05-08."
    )
    # Le wrapper try/except au top-level doit appeler le handler
    assert re.search(r"try:\s*\n\s*code\s*=\s*run\(\)", contenu), (
        "Le run() de main.py doit être wrappé dans try/except au top-level avec "
        "appel à _notifier_crash_telegram. Cf. lessons.md 2026-05-08."
    )
