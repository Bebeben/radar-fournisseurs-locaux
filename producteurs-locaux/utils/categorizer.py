"""Classification automatique des producteurs en catégories."""

import unicodedata

from config import CATEGORIES


def _normalize(text: str) -> str:
    """Minuscule, sans accents."""
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text


def categorize(text: str) -> tuple[str, str]:
    """Détermine (catégorie, sous-catégorie) à partir d'un texte produit.

    Returns:
        (catégorie principale, sous-catégorie) ou ("Autre", "Autre")
    """
    if not text:
        return "Autre", "Autre"

    norm = _normalize(text)
    best_cat = ""
    best_sub = ""
    best_score = 0

    for cat, subcats in CATEGORIES.items():
        for subcat, keywords in subcats.items():
            score = sum(1 for kw in keywords if _normalize(kw) in norm)
            if score > best_score:
                best_score = score
                best_cat = cat
                best_sub = subcat

    if best_score > 0:
        return best_cat, best_sub
    return "Autre", "Autre"


def categorize_records(records: list[dict]):
    """Ajoute catégorie et sous-catégorie aux enregistrements sans catégorie."""
    for rec in records:
        if rec.get("categorie") and rec["categorie"] != "Autre":
            continue
        text = " ".join(filter(None, [
            rec.get("produits", ""),
            rec.get("nom", ""),
            rec.get("sous_categorie", ""),
        ]))
        cat, sub = categorize(text)
        rec["categorie"] = cat
        rec["sous_categorie"] = sub
