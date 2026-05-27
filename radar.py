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
    "boulangerie_patisserie": "orange",
    "fruits_legumes_jus": "green",
    "boissons_biere_cidre_vin": "purple",
    "epicerie_huile_sucre": "darkgreen",
    "agriculture_ferme": "cadetblue",
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

def calculer_score(p: dict) -> int:
    """Score 'esprit producteur' : prioritise les petits artisans / producteurs en direct."""
    score = 0
    # Signaux qualité
    if p.get("est_bio"): score += 2
    if p.get("est_patrimoine_vivant"): score += 3
    if p.get("est_societe_mission"): score += 1
    # Forme juridique : entrepreneur individuel = très probablement un vrai producteur fermier
    if p.get("est_entrepreneur_individuel"): score += 2
    # Taille : petit = pertinent
    tranche = p.get("tranche_effectif", "")
    if tranche in ("00", "01", "02", "03"):  # 0 à 9 salariés
        score += 1
    # Labels (chaque label matché = +1)
    for k in ("label_agence_bio", "label_bienvenue_ferme", "label_cducentre",
              "label_marque_parc_brenne", "label_inao"):
        if p.get(k):
            score += 1
    return score


# ====================================================================
# Fusion labels <-> SIRENE
# ====================================================================

import re as _re_match


def _noms_alternatifs(nom: str) -> list[str]:
    """Renvoie les variantes utiles d'un nom pour le matching :
    - le nom complet
    - le contenu de chaque parenthèse (nom commercial dans SIRENE : "DUPONT (LA BRASSERIE VERTE)")
    - le préfixe avant la première parenthèse (raison sociale brute)
    """
    if not nom:
        return []
    nom = nom.lower()
    variantes = {nom}
    # Contenu des parenthèses
    for m in _re_match.findall(r"\(([^)]+)\)", nom):
        variantes.add(m.strip())
    # Préfixe avant la première parenthèse
    if "(" in nom:
        variantes.add(nom.split("(")[0].strip())
    return [v for v in variantes if v]


def matcher_label_sur_producteurs(producteurs: list[dict], items_label: list[dict], cle_label: str) -> None:
    """Modifie producteurs en place : ajoute un drapeau `cle_label` + l'URL de fiche label.

    Matching robuste : on essaie toutes les variantes du nom SIRENE (raison sociale brute,
    nom commercial entre parenthèses, nom complet) contre le nom du label.
    Bonus si même commune.
    """
    for item in items_label:
        nom_l = (item.get("nom") or "").lower()
        commune_l = (item.get("commune") or "").lower()
        siret_l = (item.get("siret") or "")
        if not nom_l:
            continue
        best_idx, best_score = -1, 0
        for i, p in enumerate(producteurs):
            if siret_l and p.get("siret") == siret_l:
                best_idx, best_score = i, 100
                break
            commune_p = (p.get("commune") or "").lower()
            # On teste TOUTES les variantes du nom SIRENE
            variantes = _noms_alternatifs(p.get("nom_complet") or "")
            for variante in variantes:
                score = fuzz.token_set_ratio(nom_l, variante)
                if commune_l and commune_p and commune_l == commune_p:
                    score = min(100, score + 10)
                if score > best_score:
                    best_idx, best_score = i, score
        if best_idx >= 0 and best_score >= 85:
            producteurs[best_idx][cle_label] = True
            url_fiche = item.get("url_fiche")
            if url_fiche:
                producteurs[best_idx][f"url_{cle_label}"] = url_fiche


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
                matcher_label_sur_producteurs(producteurs, items, cle_label)
                _log(f"[{nom_source}] {len(items)} items, matchés sur SIRENE")
        except Exception as e:
            _log(f"[regions] erreur: {e}")

    # 5. Score
    for p in producteurs:
        p["score_pertinence"] = calculer_score(p)

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
        "site_web", "telephone", "email", "fiche_annuaire",
        "code_naf", "libelle_naf", "categorie_entreprise", "tranche_effectif",
        "est_bio", "est_patrimoine_vivant", "est_entrepreneur_individuel",
        "score_pertinence", "dirigeant_principal", "siren", "siret",
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
        # Compte des producteurs pour mettre le total dans le nom de la couche
        n = int((df["categorie"] == cat).sum())
        nom_groupe = f"{cat} ({n})"
        # MarkerCluster propre à chaque catégorie pour ne pas charger la carte
        fg = folium.FeatureGroup(name=nom_groupe, show=True)
        cluster_cat = MarkerCluster().add_to(fg)
        groupes_par_cat[cat] = cluster_cat
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
        # Labels avec lien vers la fiche détaillée quand dispo
        labels_html = []
        def _fmt_label(libelle: str, cle_label: str):
            url = r.get(f"url_{cle_label}", "")
            if isinstance(url, str) and url:
                return f"<a href='{url}' target='_blank'>{libelle}</a>"
            return libelle
        if r.get("est_bio"): labels_html.append("Bio (SIRENE)")
        if r.get("label_agence_bio"): labels_html.append(_fmt_label("Agence Bio", "label_agence_bio"))
        if r.get("label_cducentre"): labels_html.append(_fmt_label("© du Centre", "label_cducentre"))
        if r.get("label_marque_parc_brenne"): labels_html.append(_fmt_label("Marque Parc Brenne", "label_marque_parc_brenne"))
        # Labels régionaux génériques (les autres labels régionaux scrapés)
        for col in r.index if hasattr(r, "index") else r.keys():
            if isinstance(col, str) and col.startswith("label_") and r.get(col) and col not in {
                "label_agence_bio", "label_cducentre", "label_marque_parc_brenne",
                "label_bienvenue_ferme", "label_inao",
            }:
                # Extraire le nom lisible
                nom_label = col.replace("label_", "").replace("_", " ").title()
                labels_html.append(_fmt_label(nom_label, col))
        if r.get("label_inao"): labels_html.append(_fmt_label("AOP/IGP (INAO)", "label_inao"))
        if r.get("est_patrimoine_vivant"): labels_html.append("EPV")
        flags_str = " · ".join(labels_html) if labels_html else "-"
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

        popup = (f"<b>{r.get('nom_complet','')}</b><br>"
                 f"<i>{cat}</i> — {r.get('distance_km','?')} km<br>"
                 f"{r.get('adresse','')}, {r.get('code_postal','')} {r.get('commune','')}<br>"
                 + (f"{contact_str}<br>" if contact_str else "")
                 + f"NAF : {r.get('code_naf','')} {r.get('libelle_naf','')}<br>"
                 f"Labels : {flags_str}<br>"
                 f"Dirigeant : {r.get('dirigeant_principal','')}<br>"
                 f"Score : {r.get('score_pertinence',0)}")
        target = groupes_par_cat.get(cat)
        if target is None:
            # fallback (catégorie absente du df.unique pour une raison X) → on l'ajoute directement à la carte
            target = m
        folium.Marker([lat, lon], popup=folium.Popup(popup, max_width=350),
                      icon=folium.Icon(color=couleur, icon="leaf", prefix="fa")).add_to(target)

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
