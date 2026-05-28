"""Radar Fournisseurs Locaux — core.

Usage CLI :
    python radar.py --config config.yaml
    python radar.py --ville saint_benoit_du_sault
    python radar.py --ville les_pieux --rayon 25

Pipeline :
1. Charge config + presets ville
2. Géocode l'adresse si besoin (api-adresse.data.gouv.fr)
3. Récupère les producteurs depuis chaque source activée :
   - SIRENE (recherche-entreprises) par (departement × code NAF)
   - Agence Bio par département
   - Bienvenue à la Ferme / © du Centre / Marque Parc Brenne par scraping
4. Filtre par distance haversine + taille + holding
5. Dédoublonne par SIREN (ou fuzzy nom+commune si pas de SIREN)
6. Calcule score_pertinence
7. Sorties Excel + CSV + carte HTML + GeoJSON
"""
from __future__ import annotations
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml
import pandas as pd
import folium
from folium.plugins import MarkerCluster
from rapidfuzz import fuzz

from sources import sirene, labels, cache_util

CATEGORIES_COULEURS = {
    "fromage_laitier": "blue",
    "viande_charcuterie": "red",
    "biscuiterie_chocolat_pates": "orange",
    "fruits_legumes_jus": "green",
    "boissons_biere_cidre": "lightred",
    "vin_viticulture": "darkpurple",
    "epicerie_huile_sucre": "darkgreen",
    "agriculture_ferme": "cadetblue",
    "aquaculture_poisson": "lightblue",
    "champignons_forestier": "beige",
    "inconnu": "gray",
}


# ====================================================================
# Configuration
# ====================================================================

def charger_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def appliquer_preset(config: dict, villes: dict) -> dict:
    preset = config.get("magasin", {}).get("preset")
    if preset and preset in villes:
        v = villes[preset]
        config["magasin"]["nom"] = v.get("nom", config["magasin"].get("nom", ""))
        config["magasin"]["adresse"] = v.get("adresse", config["magasin"].get("adresse", ""))
        if "latitude" in v:
            config["magasin"]["latitude"] = v["latitude"]
        if "longitude" in v:
            config["magasin"]["longitude"] = v["longitude"]
        config["rayon_km"] = v.get("rayon_km", config.get("rayon_km", 30))
        config["departements"] = v.get("departements", config.get("departements", []))
    return config


# ====================================================================
# Géocodage (api-adresse.data.gouv.fr, sans clé)
# ====================================================================

def geocoder(adresse: str) -> tuple[float, float] | None:
    if not adresse:
        return None
    try:
        r = requests.get(
            "https://api-adresse.data.gouv.fr/search/",
            params={"q": adresse, "limit": 1}, timeout=10,
        )
        feats = (r.json() or {}).get("features") or []
        if not feats:
            return None
        lon, lat = feats[0]["geometry"]["coordinates"]
        return (lat, lon)
    except Exception:
        return None


# ====================================================================
# Distance haversine
# ====================================================================

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


# ====================================================================
# Filtres anti-bruit
# ====================================================================

CODES_NAF_EXCLUS = {"70.10Z"}      # holdings
PREFIXES_NAF_EXCLUS = ("68.",)     # immobilier


def passe_filtres(p: dict, filtres: dict) -> tuple[bool, str]:
    """Renvoie (gardé, raison_si_exclu).

    Esprit "petit producteur / artisan" :
    - on coupe à effectif > 20 sur les transformateurs (10.xx, 11.xx)
    - on garde tout sur 01.xx (agricole), peu importe l'effectif (GAEC peuvent être gros)
    """
    if filtres.get("garder_uniquement_actifs", True):
        if p.get("etat_administratif") and p["etat_administratif"] != "A":
            return False, "inactif"
        # Filtre supplémentaire : l'établissement LOCAL (dans le dpt cherché) doit être actif aussi.
        # Sans ça, on remontait des entreprises actives globalement mais dont l'étab local est fermé.
        etat_local = p.get("etat_etab_local", "A")
        if etat_local and etat_local != "A":
            return False, "etablissement_local_ferme"

    naf = p.get("code_naf", "")
    if naf in CODES_NAF_EXCLUS:
        return False, "holding"
    if naf.startswith(PREFIXES_NAF_EXCLUS):
        return False, "immobilier"

    if filtres.get("exclure_grandes_entreprises", True):
        if p.get("categorie_entreprise") in ("ETI", "GE"):
            return False, "trop_grand"

    # Pour les transformateurs (10.xx, 11.xx), on coupe l'effectif
    if (naf.startswith("10.") or naf.startswith("11.")):
        effectif_max = filtres.get("effectif_max_transfo", 20)
        tranche = p.get("tranche_effectif", "")
        # Tranches INSEE :
        #   NN inconnu, 00=0 sal., 01=1-2, 02=3-5, 03=6-9,
        #   11=10-19, 12=20-49, 21=50-99, 22=100-199, 31=200-249, 32=250-499,
        #   41=500-999, 42=1000-1999, 51=2000-4999, 52=5000-9999, 53>=10000
        tranches_grandes = {"21", "22", "31", "32", "41", "42", "51", "52", "53"}
        if effectif_max <= 20:
            tranches_grandes |= {"12"}  # 20-49 salariés
        if effectif_max <= 10:
            tranches_grandes |= {"11"}  # 10-19 salariés
        if tranche in tranches_grandes:
            return False, "trop_gros_transfo"

    return True, ""


def categorie_pour_naf(code_naf: str, mapping: dict) -> str:
    for cat, codes in mapping.items():
        if code_naf in codes:
            return cat
    return "inconnu"


# ====================================================================
# Scoring
# ====================================================================

def calculer_score(p: dict) -> tuple[int, list[str]]:
    """Score 'esprit producteur'. Renvoie (score, détail des points attribués)."""
    score = 0
    detail = []
    if p.get("est_bio"):
        score += 2; detail.append("Bio SIRENE +2")
    if p.get("est_patrimoine_vivant"):
        score += 3; detail.append("EPV +3")
    if p.get("est_societe_mission"):
        score += 1; detail.append("Société à mission +1")
    if p.get("est_entrepreneur_individuel"):
        score += 2; detail.append("Entrepreneur individuel +2")
    tranche = p.get("tranche_effectif", "")
    if tranche in ("00", "01", "02", "03"):
        score += 1; detail.append("Petite taille (≤9 sal.) +1")
    # +1 par label matché (présence confirmée sur un site label, avec lien dans le popup)
    for k in p:
        if isinstance(k, str) and k.startswith("label_") and p.get(k):
            nom_l = k.replace("label_", "").replace("_", " ")
            score += 1
            detail.append(f"{nom_l} +1")
    return score, detail


# ====================================================================
# Fusion labels <-> SIRENE
# ====================================================================

import re as _re_match

# Préfixes juridiques / civilités à virer pour la comparaison
_PREFIXES_BRUIT = _re_match.compile(
    r"\b(gaec|earl|scea|sarl|sas|sasu|sci|sa|eurl|sce|ei|"
    r"mr|mme|monsieur|madame|"
    r"ferme(?:\s+de(?:s)?|\s+du|\s+la|\s+le|\s+les)?|"
    r"domaine(?:\s+de(?:s)?|\s+du|\s+la|\s+le|\s+les)?)\b",
    _re_match.IGNORECASE,
)


def _normaliser_nom(nom: str) -> str:
    """Normalisation pour le matching :
    - minuscules
    - tirets/apostrophes → espaces (AUPETIT-DUBOIS = AUPETIT DUBOIS pour le fuzzy)
    - retrait des préfixes juridiques (GAEC, EARL...) et civilités (Mr, Mme...)
    - retrait de la ponctuation autre que lettres/chiffres/espaces
    """
    if not nom:
        return ""
    n = nom.lower()
    # Tirets et apostrophes → espaces avant suppression des préfixes
    n = n.replace("-", " ").replace("'", " ").replace("'", " ")
    n = _PREFIXES_BRUIT.sub(" ", n)
    n = _re_match.sub(r"[^\w\s]", " ", n)
    n = _re_match.sub(r"\s+", " ", n).strip()
    return n


def _noms_alternatifs(nom: str) -> list[str]:
    """Renvoie les variantes utiles d'un nom pour le matching :
    - le nom complet normalisé
    - le contenu de chaque parenthèse normalisé (nom commercial)
    - le préfixe avant la première parenthèse normalisé
    """
    if not nom:
        return []
    nom_lower = nom.lower()
    variantes = {nom_lower, _normaliser_nom(nom)}
    for m in _re_match.findall(r"\(([^)]+)\)", nom_lower):
        variantes.add(m.strip())
        variantes.add(_normaliser_nom(m))
    if "(" in nom_lower:
        avant = nom_lower.split("(")[0].strip()
        variantes.add(avant)
        variantes.add(_normaliser_nom(avant))
    # Filtre : on garde uniquement les variantes d'au moins 4 caractères (sinon faux positifs)
    return [v for v in variantes if v and len(v) >= 4]


def _tokens_significatifs(nom_normalise: str) -> set[str]:
    """Renvoie les tokens significatifs d'un nom (mots ≥ 4 caractères, hors mots communs)."""
    mots_communs = {"ferme", "domaine", "maison", "gaec", "earl", "sarl", "scea", "ei",
                    "saint", "sainte", "saintes", "saints"}
    return {t for t in nom_normalise.split() if len(t) >= 4 and t not in mots_communs}


def matcher_label_sur_producteurs(producteurs: list[dict], items_label: list[dict], cle_label: str) -> int:
    """Tag les producteurs SIRENE qui correspondent à un item label.

    Règle stricte (anti-faux-positifs) : on tag UN producteur SIRENE pour UN item label,
    et seulement si une de ces conditions est vraie :
      - Match SIRET (sûr à 100%)
      - Même commune ET ≥ 1 token significatif commun ET fuzz token_set_ratio ≥ 70
      - Fuzz token_set_ratio ≥ 95 (match quasi-exact même sans commune)

    "Token significatif" = mot ≥ 4 chars, hors mots communs (Ferme, GAEC, Saint...).
    Ex : "Maison Apicole Oizon" (label) vs "OIZON HERVE" (SIRENE) → token commun "oizon"
         + même commune → match.
    Renvoie le nombre de tags appliqués.
    """
    n_tag = 0
    for item in items_label:
        nom_l_raw = item.get("nom") or ""
        nom_l = _normaliser_nom(nom_l_raw)
        commune_l = (item.get("commune") or "").lower().strip()
        siret_l = (item.get("siret") or "")
        if not nom_l or len(nom_l) < 4:
            continue
        # Garde-fou : sans commune ET sans SIRET, on ne peut pas matcher de façon fiable
        # (les noms seuls produisent des faux positifs). On skip cet item.
        if not commune_l and not siret_l:
            continue
        tokens_l = _tokens_significatifs(nom_l)

        for i, p in enumerate(producteurs):
            if siret_l and p.get("siret") == siret_l:
                # Match SIRET définitif
                _appliquer_tag(p, cle_label, item)
                n_tag += 1
                break
        else:
            # Pas de match SIRET — fallback nom + commune
            best_idx, best_score = -1, 0
            for i, p in enumerate(producteurs):
                commune_p = (p.get("commune") or "").lower().strip()
                meme_commune = bool(commune_l and commune_p and commune_l == commune_p)

                variantes = _noms_alternatifs(p.get("nom_complet") or "")
                for variante in variantes:
                    if len(variante) < 4:
                        continue
                    tokens_p = _tokens_significatifs(_normaliser_nom(variante))
                    score = fuzz.token_set_ratio(nom_l, _normaliser_nom(variante))

                    # Critère strict
                    accepte = False
                    if score >= 95:
                        accepte = True  # match quasi-exact
                    elif meme_commune and score >= 70 and (tokens_l & tokens_p):
                        accepte = True  # même commune + token distinctif commun

                    if accepte and score > best_score:
                        best_idx, best_score = i, score

            if best_idx >= 0:
                _appliquer_tag(producteurs[best_idx], cle_label, item)
                n_tag += 1
    return n_tag


def _appliquer_tag(producteur: dict, cle_label: str, item: dict) -> None:
    """Applique le tag label + l'URL + le nom scrapé sur un producteur."""
    producteur[cle_label] = True
    if item.get("url_fiche"):
        producteur[f"url_{cle_label}"] = item["url_fiche"]
    producteur[f"nom_label_{cle_label.replace('label_', '')}"] = item.get("nom", "")


def ajouter_producteurs_label_orphelins(producteurs: list[dict], items_label: list[dict],
                                         cle_label: str, mag_lat: float, mag_lon: float,
                                         rayon_km: float, categorie: str = "inconnu",
                                         verifier_actifs: bool = True) -> int:
    """Ajoute les items label qui n'ont matché aucun producteur SIRENE.
    Filtre par distance si l'item a des coordonnées.
    Si verifier_actifs=True ET l'item a un SIRET, vérifie qu'il est actif (évite les fermés).
    """
    from sources import sirene as _sirene
    n_ajoutes = 0
    n_fermes = 0
    existants_noms = {(p.get("nom_complet") or "").lower() for p in producteurs}
    for item in items_label:
        nom = item.get("nom") or ""
        if not nom or nom.lower() in existants_noms:
            continue
        lat, lon = item.get("latitude"), item.get("longitude")
        dist = None
        if lat and lon:
            try:
                dist = haversine_km(mag_lat, mag_lon, float(lat), float(lon))
                if dist > rayon_km:
                    continue
            except (TypeError, ValueError):
                continue
        else:
            pass

        # Vérification d'activité via SIRENE si on a le SIRET (anti-fantômes type Baritaud)
        siret = item.get("siret", "")
        if verifier_actifs and siret:
            actif = _sirene.verifier_siret_actif(siret)
            if actif is False:
                n_fermes += 1
                continue
        url_fiche = item.get("url_fiche", "")
        nouveau = {
            "siren": "",
            "siret": item.get("siret", ""),
            "nom_complet": nom,
            "code_naf": "",
            "libelle_naf": "",
            "categorie_entreprise": "",
            "tranche_effectif": "",
            "etat_administratif": "",
            "adresse": "",
            "commune": item.get("commune", ""),
            "code_postal": item.get("code_postal", ""),
            "latitude": lat,
            "longitude": lon,
            "est_bio": cle_label == "label_agence_bio",
            "est_patrimoine_vivant": False,
            "est_societe_mission": False,
            "est_ess": False,
            "est_entrepreneur_individuel": False,
            "dirigeant_principal": "",
            "site_web": "",
            "telephone": "",
            "email": "",
            "fiche_annuaire": "",
            "categorie": categorie,
            "distance_km": dist,
            "source_label_seul": True,
            cle_label: True,
            f"url_{cle_label}": url_fiche,
        }
        producteurs.append(nouveau)
        n_ajoutes += 1
    if n_fermes:
        # Note : on aurait pu logger via _log mais cette fonction n'y a pas accès,
        # le compteur est juste un retour secondaire.
        pass
    return n_ajoutes


# ====================================================================
# Pipeline principal
# ====================================================================

def run(config: dict, naf_map: dict, verbose: bool = True, log_cb=None) -> pd.DataFrame:
    """Pipeline principal. Si log_cb est fourni, c'est une fonction appelée pour chaque
    message de progression (au lieu d'un print stdout). Permet d'afficher la progression
    dans Streamlit Cloud où le terminal n'est pas visible côté utilisateur."""
    def _log(msg: str):
        if log_cb:
            log_cb(msg)
        elif verbose:
            print(msg, flush=True)
    mag = config["magasin"]
    rayon = config.get("rayon_km", 30)
    deps = config.get("departements", [])
    filtres = config.get("filtres", {})
    sources_actives = config.get("sources", {})
    cache_dir = config.get("cache", {}).get("dossier", "cache")
    ttl = config.get("cache", {}).get("ttl_jours", 7)

    # 1. Coordonnées du magasin
    lat = mag.get("latitude")
    lon = mag.get("longitude")
    if not (lat and lon):
        if verbose:
            _log(f"[geocode] {mag.get('adresse','')}")
        coords = geocoder(mag.get("adresse", ""))
        if not coords:
            raise RuntimeError("Impossible de géocoder l'adresse du magasin.")
        lat, lon = coords
        mag["latitude"], mag["longitude"] = lat, lon
    if verbose:
        _log(f"[magasin] {mag.get('nom')} @ ({lat:.4f}, {lon:.4f}) rayon={rayon}km dep={deps}")

    # 2. Catégories actives et codes NAF
    categories_actives = [c for c, on in config.get("categories", {}).items() if on]
    codes_naf = []
    code_to_cat = {}
    for cat in categories_actives:
        for code in naf_map.get(cat, []):
            codes_naf.append(code)
            code_to_cat[code] = cat
    if verbose:
        _log(f"[naf] {len(codes_naf)} codes NAF interrogés, {len(categories_actives)} catégories")

    # 3. Source SIRENE
    producteurs = []
    exclus = {"trop_grand": 0, "holding": 0, "immobilier": 0,
              "inactif": 0, "semi_industriel": 0, "hors_rayon": 0, "sans_geo": 0}

    if sources_actives.get("sirene", True):
        cache_key = f"sirene_{'_'.join(sorted(deps))}_{'_'.join(sorted(codes_naf))}"
        cached = cache_util.load(cache_dir, cache_key, ttl)
        if cached is not None:
            results = cached
            _log(f"[sirene] cache hit ({len(results)} brut)")
        else:
            _log(f"[sirene] interrogation API ({len(codes_naf)} codes × {len(deps)} dpts, 4 workers parallèles)...")
            def _cb(i, total, code, n):
                if verbose:
                    print(f"  [{i}/{total}] {code} -> +{n} nouveaux", flush=True)
            results = sirene.chercher_multi(codes_naf, deps, progress_cb=_cb)
            cache_util.save(cache_dir, cache_key, results)
            _log(f"[sirene] {len(results)} résultats bruts")

        for r in results:
            p = sirene.extraire_normalise(r)
            p["categorie"] = categorie_pour_naf(p["code_naf"], naf_map)
            ok, raison = passe_filtres(p, filtres)
            if not ok:
                exclus[raison] = exclus.get(raison, 0) + 1
                continue
            if not (p.get("latitude") and p.get("longitude")):
                exclus["sans_geo"] += 1
                continue
            try:
                d = haversine_km(lat, lon, float(p["latitude"]), float(p["longitude"]))
            except (TypeError, ValueError):
                exclus["sans_geo"] += 1
                continue
            if d > rayon:
                exclus["hors_rayon"] += 1
                continue
            p["distance_km"] = round(d, 2)
            producteurs.append(p)

    if verbose:
        _log(f"[sirene] {len(producteurs)} producteurs gardés après filtres + rayon")
        _log(f"[exclus] {exclus}")

    # 4. Sources labels
    # Source nationale : Agence Bio
    if sources_actives.get("agence_bio", True):
        try:
            items = labels.agence_bio(deps, cache_dir, ttl)
            matcher_label_sur_producteurs(producteurs, items, "label_agence_bio")
            n = ajouter_producteurs_label_orphelins(producteurs, items, "label_agence_bio",
                                                    lat, lon, rayon)
            _log(f"[agence_bio] {len(items)} items, {n} orphelins ajoutés")
        except Exception as e:
            _log(f"[agence_bio] erreur: {e}")

    # Source nationale : INAO AOP/IGP
    if sources_actives.get("inao", True):
        try:
            items = labels.inao(cache_dir, ttl)
            matcher_label_sur_producteurs(producteurs, items, "label_inao")
            _log(f"[inao] {len(items)} items matchés")
        except Exception as e:
            _log(f"[inao] erreur: {e}")

    # Sources régionales (déclaratives YAML)
    if sources_actives.get("regionales", True):
        try:
            par_source = labels.sources_regionales(deps, cache_dir, ttl, verbose=verbose)
            for nom_source, items in par_source.items():
                cle_label = f"label_{nom_source}"
                # Matching strict : un item label se rattache à UN producteur SIRENE max,
                # par SIRET (fiable) ou par même commune + tokens distinctifs communs
                n_tag = matcher_label_sur_producteurs(producteurs, items, cle_label)
                # Items qui n'ont pas matché → ajoutés en orphelins si dans le rayon
                n_orph = ajouter_producteurs_label_orphelins(
                    producteurs, items, cle_label, lat, lon, rayon
                )
                _log(f"[{nom_source}] {len(items)} items : {n_tag} tagués sur SIRENE, {n_orph} orphelins ajoutés")
        except Exception as e:
            _log(f"[regions] erreur: {e}")

    # 5. Score + colonnes consolidées labels (lisibles + lien cliquable)
    for p in producteurs:
        score, detail = calculer_score(p)
        p["score_pertinence"] = score
        p["score_detail"] = " · ".join(detail) if detail else "(aucun signal)"

        # Consolidation des labels matchés : texte + première URL pour la colonne cliquable
        labels_noms = []
        premiere_url = ""
        for k in list(p.keys()):
            if isinstance(k, str) and k.startswith("label_") and p.get(k):
                nom_lbl = k.replace("label_", "").replace("_", " ")
                labels_noms.append(nom_lbl)
                url = p.get(f"url_{k}", "")
                if url and not premiere_url:
                    premiere_url = url
        p["labels"] = " · ".join(labels_noms) if labels_noms else ""
        p["fiche_label"] = premiere_url

    # 5bis. Mode premium : on coupe les producteurs sans aucun signal de vente directe
    mode = filtres.get("mode", "premium")
    if mode == "premium":
        seuil = filtres.get("score_min_premium", 1)
        avant = len(producteurs)
        producteurs = [p for p in producteurs if p.get("score_pertinence", 0) >= seuil]
        if verbose:
            _log(f"[mode={mode}] {len(producteurs)}/{avant} producteurs gardés (score >= {seuil})")

    # 7. DataFrame trié — score décroissant puis distance croissante (les meilleurs en haut)
    df = pd.DataFrame(producteurs)
    if not df.empty:
        # IMPORTANT : remplir les colonnes label_* avec False (sinon NaN, et `if NaN` est True
        # en Python → bug d'affichage où tous les producteurs apparaissent dans chaque filtre label)
        for col in df.columns:
            if isinstance(col, str) and col.startswith("label_"):
                df[col] = df[col].fillna(False).astype(bool)
        df = df.sort_values(
            ["score_pertinence", "distance_km"],
            ascending=[False, True], na_position="last",
        ).reset_index(drop=True)
        df["a_contacter"] = ""
    return df


# ====================================================================
# Sorties
# ====================================================================

def export_excel(df: pd.DataFrame, path: str) -> None:
    if df.empty:
        df.to_excel(path, index=False)
        return
    colonnes_principales = [
        "categorie", "nom_complet", "distance_km", "commune", "code_postal", "adresse",
        "labels", "fiche_label",
        "site_web", "telephone", "email", "fiche_annuaire",
        "code_naf", "libelle_naf", "categorie_entreprise", "tranche_effectif",
        "est_bio", "est_patrimoine_vivant", "est_entrepreneur_individuel",
        "score_pertinence", "score_detail", "dirigeant_principal", "siren", "siret",
        "source_label_seul", "a_contacter",
    ]
    # Colonnes labels (booléens) — toujours présentes même vides
    colonnes_labels = sorted([c for c in df.columns if c.startswith("label_")])
    # Colonnes URL fiches label
    colonnes_urls = sorted([c for c in df.columns if c.startswith("url_label_")])

    presentes = [c for c in colonnes_principales if c in df.columns]
    autres = [c for c in df.columns
              if c not in presentes and c not in colonnes_labels and c not in colonnes_urls]
    df_out = df[presentes + colonnes_labels + colonnes_urls + autres]
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df_out.to_excel(w, index=False, sheet_name="Producteurs")
        ws = w.sheets["Producteurs"]
        # Filtres auto + ligne en-tête en gras
        ws.auto_filter.ref = ws.dimensions
        from openpyxl.styles import Font
        for cell in ws[1]:
            cell.font = Font(bold=True)


def export_csv(df: pd.DataFrame, path: str) -> None:
    df.to_csv(path, index=False, encoding="utf-8-sig")


def export_geojson(df: pd.DataFrame, path: str) -> None:
    features = []
    for _, r in df.iterrows():
        if pd.isna(r.get("latitude")) or pd.isna(r.get("longitude")):
            continue
        try:
            lat = float(r["latitude"]); lon = float(r["longitude"])
        except (TypeError, ValueError):
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {k: (None if pd.isna(v) else v) for k, v in r.items()
                           if k not in ("latitude", "longitude")},
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, ensure_ascii=False)


def export_carte(df: pd.DataFrame, mag_lat: float, mag_lon: float, mag_nom: str,
                 rayon_km: float, path: str) -> None:
    m = folium.Map(location=[mag_lat, mag_lon], zoom_start=10, tiles="OpenStreetMap")
    folium.Marker(
        [mag_lat, mag_lon],
        popup=folium.Popup(f"<b>{mag_nom}</b><br>Magasin", max_width=300),
        icon=folium.Icon(color="black", icon="shopping-cart", prefix="fa"),
    ).add_to(m)
    folium.Circle([mag_lat, mag_lon], radius=rayon_km * 1000, color="black",
                  fill=False, weight=2, dash_array="5,5").add_to(m)

    # FeatureGroup par catégorie : un groupe de marqueurs par catégorie,
    # chacun activable/désactivable depuis le LayerControl en haut à droite de la carte.
    if df.empty:
        cats_uniques = []
    else:
        cats_uniques = sorted(df["categorie"].dropna().unique().tolist())
    groupes_par_cat = {}
    for cat in cats_uniques:
        couleur = CATEGORIES_COULEURS.get(cat, "gray")
        n = int((df["categorie"] == cat).sum())
        nom_groupe = f"{cat} ({n})"
        fg = folium.FeatureGroup(name=nom_groupe, show=True)
        cluster_cat = MarkerCluster().add_to(fg)
        groupes_par_cat[cat] = cluster_cat
        m.add_child(fg)

    # FeatureGroup additionnels par label matché.
    # Un producteur peut avoir plusieurs labels → il apparaîtra dans plusieurs FG.
    cols_labels = sorted(set(
        c for c in df.columns if isinstance(c, str) and c.startswith("label_")
    ))
    groupes_par_label = {}
    for col in cols_labels:
        n_label = int(df[col].fillna(False).astype(bool).sum())
        if n_label == 0:
            continue
        nom_lbl = col.replace("label_", "").replace("_", " ")
        fg = folium.FeatureGroup(name=f"🏷 {nom_lbl} ({n_label})", show=False)
        groupes_par_label[col] = fg
        m.add_child(fg)

    for _, r in df.iterrows():
        if pd.isna(r.get("latitude")) or pd.isna(r.get("longitude")):
            continue
        try:
            lat = float(r["latitude"]); lon = float(r["longitude"])
        except (TypeError, ValueError):
            continue
        cat = r.get("categorie") or "inconnu"
        couleur = CATEGORIES_COULEURS.get(cat, "gray")
        # Drapeaux qualité SIRENE
        flags_sirene = []
        if r.get("est_bio"): flags_sirene.append("Bio SIRENE")
        if r.get("est_patrimoine_vivant"): flags_sirene.append("EPV")
        if r.get("est_entrepreneur_individuel"): flags_sirene.append("EI")
        flags_sirene_str = " · ".join(flags_sirene) if flags_sirene else "—"

        # Labels matchés (uniquement ceux qu'on a vraiment liés à ce producteur)
        labels_matches = []
        cols = list(r.index) if hasattr(r, "index") else list(r.keys())
        for col in cols:
            if isinstance(col, str) and col.startswith("label_") and r.get(col):
                nom_lbl = col.replace("label_", "").replace("_", " ")
                url = r.get(f"url_{col}", "")
                nom_scrape = r.get(f"nom_label_{col.replace('label_', '')}", "")
                lien = f"<a href='{url}' target='_blank'>{nom_scrape or nom_lbl}</a>" if url else (nom_scrape or nom_lbl)
                labels_matches.append(f"<b>{nom_lbl}</b> : {lien}")
        cand_str = "<br>".join(labels_matches) if labels_matches else "<i>aucun label matché</i>"
        site = r.get("site_web", "")
        tel = r.get("telephone", "")
        annuaire = r.get("fiche_annuaire", "")
        lignes_contact = []
        if site:
            url_site = site if site.startswith("http") else "https://" + site
            lignes_contact.append(f"🌐 <a href='{url_site}' target='_blank'>{site}</a>")
        if tel:
            lignes_contact.append(f"☎ {tel}")
        if annuaire:
            lignes_contact.append(f"<a href='{annuaire}' target='_blank'>Fiche annuaire-entreprises</a>")
        contact_str = "<br>".join(lignes_contact) if lignes_contact else ""

        detail_score = (r.get("score_detail") or "")
        popup = (
            f"<b>{r.get('nom_complet','')}</b><br>"
            f"<i>{cat}</i> — {r.get('distance_km','?')} km<br>"
            f"{r.get('adresse','')}, {r.get('code_postal','')} {r.get('commune','')}<br>"
            + (f"{contact_str}<br>" if contact_str else "")
            + f"NAF : {r.get('code_naf','')} {r.get('libelle_naf','')}<br>"
            f"Signaux SIRENE : {flags_sirene_str}<br>"
            f"<b>Labels matchés :</b><br>{cand_str}<br>"
            f"Dirigeant : {r.get('dirigeant_principal','')}<br>"
            f"<b>Score : {r.get('score_pertinence',0)}</b> "
            f"<span style='color:#888; font-size:11px'>({detail_score})</span>"
        )
        target = groupes_par_cat.get(cat)
        if target is None:
            target = m
        folium.Marker([lat, lon], popup=folium.Popup(popup, max_width=400),
                      icon=folium.Icon(color=couleur, icon="leaf", prefix="fa")).add_to(target)
        # Duplique le marqueur dans les FG des labels matchés
        for col, fg_label in groupes_par_label.items():
            if r.get(col):
                folium.Marker(
                    [lat, lon], popup=folium.Popup(popup, max_width=400),
                    icon=folium.Icon(color="black", icon="bookmark", prefix="fa"),
                ).add_to(fg_label)

    # LayerControl : cases à cocher en haut à droite pour activer/désactiver chaque catégorie
    folium.LayerControl(collapsed=False, position="topright").add_to(m)

    # Mini-script JS : ajoute des liens "Tout cocher" / "Tout décocher" au-dessus du LayerControl
    script_filtres = """
    <script>
    document.addEventListener('DOMContentLoaded', function() {
        setTimeout(function() {
            var ctrl = document.querySelector('.leaflet-control-layers-list');
            if (!ctrl) return;
            var overlays = document.querySelector('.leaflet-control-layers-overlays');
            if (!overlays) return;
            var bar = document.createElement('div');
            bar.style.cssText = 'border-bottom:1px solid #ccc; padding:4px 0; margin-bottom:4px; font-size:12px;';
            bar.innerHTML = '<a href="#" id="check-all-cats" style="color:#E2001A; margin-right:8px;">Tout cocher</a>'
                          + '<a href="#" id="uncheck-all-cats" style="color:#E2001A;">Tout décocher</a>';
            overlays.parentNode.insertBefore(bar, overlays);
            document.getElementById('check-all-cats').onclick = function(e) {
                e.preventDefault();
                overlays.querySelectorAll('input[type=checkbox]').forEach(function(cb) {
                    if (!cb.checked) cb.click();
                });
            };
            document.getElementById('uncheck-all-cats').onclick = function(e) {
                e.preventDefault();
                overlays.querySelectorAll('input[type=checkbox]').forEach(function(cb) {
                    if (cb.checked) cb.click();
                });
            };
        }, 500);
    });
    </script>
    """
    m.get_root().html.add_child(folium.Element(script_filtres))

    # Légende fixe en bas à gauche (couleurs)
    legende = "<div style='position: fixed; bottom: 30px; left: 30px; background: white; padding: 10px; border: 1px solid #888; z-index: 9999; font-size: 12px; max-width: 220px;'>"
    legende += "<b>Couleurs par catégorie</b><br>"
    for cat in cats_uniques:
        col = CATEGORIES_COULEURS.get(cat, "gray")
        legende += f"<span style='color:{col}; font-size: 18px;'>●</span> {cat}<br>"
    legende += "<hr style='margin: 5px 0'><i>Filtre par catégorie en haut à droite ↗</i>"
    legende += "</div>"
    m.get_root().html.add_child(folium.Element(legende))
    m.save(path)


# ====================================================================
# Main CLI
# ====================================================================

def main():
    ap = argparse.ArgumentParser(description="Radar Fournisseurs Locaux")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--ville", help="Clé d'un preset dans villes.yaml")
    ap.add_argument("--rayon", type=float, help="Override rayon_km")
    ap.add_argument("--no-cache", action="store_true", help="Bypass cache disque")
    args = ap.parse_args()

    config = charger_yaml(args.config)
    naf_map = charger_yaml("naf.yaml")
    villes = charger_yaml("villes.yaml")

    if args.ville:
        config["magasin"]["preset"] = args.ville
    config = appliquer_preset(config, villes)
    if args.rayon:
        config["rayon_km"] = args.rayon
    if args.no_cache:
        config.setdefault("cache", {})["ttl_jours"] = 0

    df = run(config, naf_map)
    print(f"\n=== {len(df)} producteurs retenus ===")
    if not df.empty:
        print(df.groupby("categorie")["nom_complet"].count().to_string())

    out_dir = Path(config.get("sortie", {}).get("dossier", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    horodatage = datetime.now().strftime("%Y%m%d_%H%M")
    base = f"producteurs_{(config['magasin'].get('nom','radar')).replace(' ', '_')}_{horodatage}"

    if config["sortie"].get("excel", True):
        export_excel(df, str(out_dir / f"{base}.xlsx"))
    if config["sortie"].get("csv", True):
        export_csv(df, str(out_dir / f"{base}.csv"))
    if config["sortie"].get("geojson", True):
        export_geojson(df, str(out_dir / f"{base}.geojson"))
    if config["sortie"].get("carte_html", True):
        export_carte(
            df,
            config["magasin"]["latitude"], config["magasin"]["longitude"],
            config["magasin"]["nom"], config["rayon_km"],
            str(out_dir / "carte.html"),
        )

    print(f"\nSorties dans {out_dir.resolve()}")


if __name__ == "__main__":
    main()
