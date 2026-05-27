"""Dédoublonnage des producteurs (exact + fuzzy)."""

import logging
import unicodedata

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 85  # Score minimal pour considérer comme doublon


def _normalize_key(text) -> str:
    """Normalise un texte pour la comparaison : minuscule, sans accents, sans mots courants."""
    if not isinstance(text, str):
        text = str(text) if text and text == text else ""  # Handle NaN
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    # Retirer mots courants
    for word in ["ferme", "la", "le", "les", "de", "du", "des", "l'", "d'", "sarl", "earl", "gaec", "sas"]:
        text = text.replace(word, "")
    return " ".join(text.split())  # compacter les espaces


def _make_key(record: dict) -> str:
    """Clé composite nom + ville."""
    nom = _normalize_key(record.get("nom", ""))
    ville = _normalize_key(record.get("ville", ""))
    return f"{nom}|{ville}"


def _merge_records(existing: dict, new: dict) -> dict:
    """Fusionne deux enregistrements en gardant les champs les plus complets."""
    merged = dict(existing)
    for key, val in new.items():
        if key == "source":
            sources = set(filter(None, [existing.get("source", ""), val]))
            merged["source"] = " ; ".join(sorted(sources))
        elif key == "date_collecte":
            # Garder la date la plus récente
            if val and (not merged.get(key) or val > merged.get(key, "")):
                merged[key] = val
        elif val and not merged.get(key):
            merged[key] = val
    return merged


def deduplicate(new_records: list[dict], existing_records: list[dict] | None = None) -> list[dict]:
    """Dédoublonne les enregistrements (exact puis fuzzy).

    Args:
        new_records: Nouveaux enregistrements collectés
        existing_records: Enregistrements existants (depuis Excel précédent)

    Returns:
        Liste dédoublonnée et fusionnée
    """
    all_records = (existing_records or []) + new_records

    if not all_records:
        return []

    # Phase 1 : dédoublonnage exact
    exact_groups: dict[str, dict] = {}
    unmatched: list[dict] = []

    for rec in all_records:
        key = _make_key(rec)
        if not key.replace("|", "").strip():
            unmatched.append(rec)
            continue
        if key in exact_groups:
            exact_groups[key] = _merge_records(exact_groups[key], rec)
        else:
            exact_groups[key] = dict(rec)

    # Phase 2 : dédoublonnage fuzzy sur les clés restantes
    keys = list(exact_groups.keys())
    merged_indices: set[int] = set()

    for i in range(len(keys)):
        if i in merged_indices:
            continue
        for j in range(i + 1, len(keys)):
            if j in merged_indices:
                continue
            score = fuzz.token_sort_ratio(keys[i], keys[j])
            if score >= FUZZY_THRESHOLD:
                exact_groups[keys[i]] = _merge_records(
                    exact_groups[keys[i]], exact_groups[keys[j]]
                )
                merged_indices.add(j)

    result = [
        exact_groups[keys[i]]
        for i in range(len(keys))
        if i not in merged_indices
    ]
    result.extend(unmatched)

    dedup_count = len(all_records) - len(result)
    if dedup_count > 0:
        logger.info(f"Dédoublonnage : {dedup_count} doublons fusionnés")

    return result
