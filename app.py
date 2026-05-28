"""Interface Streamlit — Radar Fournisseurs Locaux.

Lancement :
    streamlit run app.py

Pour déploiement sur Streamlit Community Cloud (gratuit) : pousser le repo sur GitHub,
puis "Deploy" depuis https://share.streamlit.io/.
"""
from __future__ import annotations
import io
import os
from datetime import datetime
from pathlib import Path

import math
import streamlit as st
import yaml
import pandas as pd
import requests

import radar


# --------- Helper : auto-détection des départements dans un rayon ----------

@st.cache_data
def charger_dpt_bbox() -> dict:
    """Charge les bounding boxes des départements (lat_min, lon_min, lat_max, lon_max)."""
    p = ROOT / "departements_bbox.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


def _haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1); dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2)
    return 2 * R * math.asin(math.sqrt(a))


def _distance_point_bbox(lat: float, lon: float, bbox: list[float]) -> float:
    """Distance minimale (km) entre un point et un rectangle bbox [lat_min, lon_min, lat_max, lon_max].
    Si le point est DANS la bbox, renvoie 0.
    Sinon, projette le point sur le côté le plus proche du rectangle et calcule haversine.
    """
    lat_min, lon_min, lat_max, lon_max = bbox
    closest_lat = max(lat_min, min(lat, lat_max))
    closest_lon = max(lon_min, min(lon, lon_max))
    return _haversine(lat, lon, closest_lat, closest_lon)


def departements_dans_rayon(lat: float, lon: float, rayon_km: float, marge_km: float = 0) -> list[str]:
    """Renvoie les codes département dont la frontière est à <= rayon_km du point magasin.
    Utilise les bounding boxes (rectangles englobants) pour une détection précise — plus de marge bidon.
    Le paramètre marge_km existe pour compat mais devrait être 0 dans le nouveau mode.
    """
    bboxes = charger_dpt_bbox()
    if not bboxes:
        return []
    seuil = rayon_km + marge_km
    out = []
    for code, bbox in bboxes.items():
        if not bbox or len(bbox) != 4:
            continue
        d = _distance_point_bbox(lat, lon, bbox)
        if d <= seuil:
            out.append((code, d))
    out.sort(key=lambda x: x[1])
    return [c for c, _ in out]


# --------- Helper : autocomplete ville ----------

@st.cache_data(ttl=3600)
def chercher_communes(query: str) -> list[dict]:
    """Appelle api-adresse.data.gouv.fr pour suggérer des communes (autocomplete)."""
    if not query or len(query) < 2:
        return []
    try:
        r = requests.get(
            "https://api-adresse.data.gouv.fr/search/",
            params={"q": query, "type": "municipality", "limit": 8},
            timeout=5,
        )
        if r.status_code != 200:
            return []
        feats = (r.json() or {}).get("features") or []
        out = []
        for f in feats:
            p = f.get("properties", {})
            g = f.get("geometry", {}).get("coordinates", [None, None])
            out.append({
                "label": f"{p.get('city', '?')} ({p.get('postcode', '?')}) — {p.get('context', '')}",
                "city": p.get("city", ""),
                "postcode": p.get("postcode", ""),
                "citycode": p.get("citycode", ""),
                "departement": (p.get("citycode") or "")[:2] if (p.get("citycode") or "").startswith("97") is False else (p.get("citycode") or "")[:3],
                "context": p.get("context", ""),
                "lat": g[1] if g and len(g) >= 2 else None,
                "lon": g[0] if g and len(g) >= 2 else None,
            })
        return out
    except Exception:
        return []


# --------------- Chargement config par défaut ----------------

ROOT = Path(__file__).parent


@st.cache_data
def charger_naf():
    return yaml.safe_load(open(ROOT / "naf.yaml", "r", encoding="utf-8"))


@st.cache_data
def charger_naf_complet():
    """Nomenclature NAF complète avec libellés humains."""
    return yaml.safe_load(open(ROOT / "naf_complet.yaml", "r", encoding="utf-8"))


@st.cache_data
def charger_villes():
    p = ROOT / "villes.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(open(p, "r", encoding="utf-8")) or {}


@st.cache_data
def charger_config_defaut():
    return yaml.safe_load(open(ROOT / "config.yaml", "r", encoding="utf-8"))


# --------------- UI ----------------

st.set_page_config(
    page_title="Radar Fournisseurs Locaux — Super U",
    layout="wide",
    page_icon="🛒",
)

# Charte couleur (rouge primaire en CSS pour boutons/liens, sans bandeau ni texte)
st.markdown("""
<style>
    h1, h2, h3 { color: #1A1A1A; }
    h2 {
        border-bottom: 3px solid #E2001A;
        padding-bottom: 6px;
        margin-top: 28px;
    }
    .stButton > button[kind="primary"] {
        background-color: #E2001A;
        border-color: #E2001A;
        color: white;
        font-weight: 600;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #B30015;
        border-color: #B30015;
    }
    a { color: #E2001A; }
    a:hover { color: #B30015; }
</style>
""", unsafe_allow_html=True)

st.title("Radar Fournisseurs Locaux")

# Lien visible en haut de page vers la page d'ajout de source (multipage Streamlit)
nav_col1, nav_col2 = st.columns([5, 2])
with nav_col2:
    st.page_link("pages/1_Ajouter_une_source.py", label="➕ Ajouter une source", icon="🔗")

st.caption("Cartographie les producteurs alimentaires autour d'un magasin U. "
           "SIRENE + Agence Bio + labels régionaux — outil interne pour l'identification de fournisseurs en vente directe.")

config_def = charger_config_defaut()
naf_map = charger_naf()
naf_complet = charger_naf_complet()
villes = charger_villes()

# --------- Sidebar : paramètres magasin ----------
with st.sidebar:
    st.header("Magasin")

    # ----- Charger un preset enregistré -----
    presets_list = list(villes.keys()) if villes else []
    if presets_list:
        col_load, col_btn = st.columns([3, 1])
        with col_load:
            preset_choisi = st.selectbox(
                "Magasins enregistrés",
                ["(nouveau)"] + presets_list,
                key="preset_select",
                label_visibility="visible",
            )
        with col_btn:
            st.write("")  # alignement
            if preset_choisi != "(nouveau)":
                if st.button("📂", help="Charger ce preset", key="load_preset"):
                    v = villes[preset_choisi]
                    st.session_state["ville_search"] = v.get("adresse", "").split(",")[0]
                    st.session_state["_preset_rayon"] = v.get("rayon_km", 30)
                    st.session_state["_preset_nom"] = v.get("nom", "")
                    st.rerun()

    query = st.text_input(
        "Commune (appuie sur Entrée après avoir tapé)",
        "Saint-Benoît-du-Sault",
        key="ville_search",
        help="Tape le nom de la commune puis appuie sur Entrée. "
             "Si une seule commune correspond, elle est sélectionnée d'office. "
             "S'il y a ambiguïté, un menu déroulant apparaît pour choisir.",
    )
    # Rayon : récupère valeur preset si on vient de charger un preset
    rayon_default = st.session_state.pop("_preset_rayon", 30) if "_preset_rayon" in st.session_state else 30
    rayon = st.slider("Rayon (km)", 5, 200, int(rayon_default))

    suggestions = chercher_communes(query)
    ville_choisie = None
    deps_auto: list[str] = []

    if not suggestions:
        if query and len(query) >= 2:
            st.warning("Aucune commune trouvée. Vérifie l'orthographe.")
        adresse = query if query else "Saint-Benoît-du-Sault, 36170"
        deps_default = "36,87,23"
        nom = f"Super U {query}" if query else "Super U"
    elif len(suggestions) == 1:
        # Pas d'ambiguïté : on prend direct
        ville_choisie = suggestions[0]
    else:
        # Plusieurs candidates : selectbox de désambiguïsation
        labels_suggest = [s["label"] for s in suggestions]
        choix_idx = st.selectbox("Plusieurs communes correspondent — choisis :",
                                  options=list(range(len(suggestions))),
                                  format_func=lambda i: labels_suggest[i],
                                  index=0, key="ville_choix")
        ville_choisie = suggestions[choix_idx]

    if ville_choisie:
        adresse = f"{ville_choisie['city']}, {ville_choisie['postcode']}"
        # Nom : valeur preset si chargée, sinon "Super U {ville}" par défaut
        nom_default = st.session_state.pop("_preset_nom", "") or f"Super U {ville_choisie['city']}"
        if ville_choisie.get("lat") and ville_choisie.get("lon"):
            deps_auto = departements_dans_rayon(
                ville_choisie["lat"], ville_choisie["lon"], rayon,
            )
        deps_default = ",".join(deps_auto) if deps_auto else ville_choisie["departement"]
        st.caption(
            f"📍 **{ville_choisie['city']}** ({ville_choisie['postcode']}) — dpt {ville_choisie['departement']} "
            f"· {len(deps_auto)} département(s) à interroger pour un rayon de {rayon} km"
        )
        nom = st.text_input("Libellé magasin", nom_default,
                              help="Apparaît sur la carte et dans le nom de fichier export. "
                                   "Pas obligé d'être 'Super U {ville}' — peux mettre 'Hyper U', 'U Express', etc.")
    else:
        nom = f"Super U {query}" if query else "Super U"

    deps_input = st.text_input(
        "Départements à interroger",
        deps_default,
        help="Détection précise par bounding box : tous les départements dont au moins "
             "une partie est dans ton rayon. Tu peux modifier la liste si besoin.",
    )

    # ----- Sauvegarder ce magasin comme preset -----
    if ville_choisie:
        with st.expander("💾 Sauvegarder ce magasin", expanded=False):
            slug_default = nom.lower().replace(" ", "_").replace("'", "").replace("-", "_")
            slug = st.text_input("Clé (identifiant interne)", slug_default,
                                  help="Sans espaces ni accents. Sert à charger le preset plus tard.")
            col_save, col_dl = st.columns(2)
            with col_save:
                if st.button("Enregistrer dans villes.yaml"):
                    nouveau = {
                        "nom": nom,
                        "adresse": adresse,
                        "latitude": ville_choisie.get("lat"),
                        "longitude": ville_choisie.get("lon"),
                        "rayon_km": rayon,
                        "departements": [d.strip() for d in deps_input.split(",") if d.strip()],
                    }
                    villes_path = ROOT / "villes.yaml"
                    villes_actuelles = yaml.safe_load(villes_path.read_text(encoding="utf-8")) if villes_path.exists() else {}
                    villes_actuelles[slug] = nouveau
                    villes_path.write_text(
                        yaml.safe_dump(villes_actuelles, allow_unicode=True, sort_keys=False),
                        encoding="utf-8",
                    )
                    charger_villes.clear()  # invalide le cache
                    st.success(f"Sauvé sous `{slug}`. Recharge la page pour le voir dans le menu.")
                    st.caption("⚠️ Sur Streamlit Cloud, la sauvegarde est éphémère "
                               "(reset à chaque redéploiement). Utilise le bouton ⬇️ pour télécharger "
                               "et committer le YAML.")
            with col_dl:
                # Pour Streamlit Cloud : permet à Benjamin de récupérer le YAML et de le commit
                villes_path = ROOT / "villes.yaml"
                if villes_path.exists():
                    st.download_button(
                        "⬇️ villes.yaml",
                        villes_path.read_bytes(),
                        file_name="villes.yaml",
                        mime="text/yaml",
                        help="Télécharge le fichier mis à jour pour le committer sur GitHub",
                    )

    with st.expander("Charger un preset (Saint-Benoît, Les Pieux, Vaucresson...)"):
        if villes:
            preset = st.selectbox("Preset", ["(aucun)"] + list(villes.keys()))
            if preset != "(aucun)" and st.button("Appliquer"):
                v = villes[preset]
                st.session_state["ville_search"] = v.get("adresse", "").split(",")[0]
                st.rerun()

    st.header("Catégories")
    st.caption("Cases cochées = groupes de codes NAF prédéfinis dans `naf.yaml`.")
    cats_actives = {}
    for cat in naf_map.keys():
        cats_actives[cat] = st.checkbox(cat, value=config_def.get("categories", {}).get(cat, True))

    # NAF individuels — sélection à la carte dans la nomenclature INSEE complète
    naf_individuels_actifs: list[str] = []
    with st.expander("➕ Codes NAF supplémentaires (à la carte)"):
        st.caption(f"Liste complète des codes alimentaires INSEE ({len(naf_complet)} codes). "
                   "Coche pour ajouter au radar, en plus des catégories ci-dessus.")
        # Filtre par préfixe pour rendre la liste navigable
        prefixes = sorted({code.split(".")[0] for code in naf_complet.keys()})
        filtre_prefix = st.multiselect(
            "Filtrer par préfixe (01=agriculture, 02=sylviculture, 03=pêche, 10=industrie alim., 11=boissons)",
            prefixes, default=[],
        )
        # Champs cherche-NAF
        recherche = st.text_input("Recherche libre (nom contient...)", "")
        # Codes déjà inclus via les catégories cochées
        deja_inclus = set()
        for cat, codes in naf_map.items():
            if cats_actives.get(cat):
                deja_inclus.update(codes)

        for code, libelle in naf_complet.items():
            if filtre_prefix and code.split(".")[0] not in filtre_prefix:
                continue
            if recherche and recherche.lower() not in libelle.lower():
                continue
            label = f"`{code}` — {libelle}"
            if code in deja_inclus:
                label += " ✓ (déjà couvert)"
            checked = st.checkbox(label, value=False, key=f"naf_indiv_{code}")
            if checked and code not in deja_inclus:
                naf_individuels_actifs.append(code)

    # Variable conservée pour compat — vide par défaut, peut être étendue plus tard
    naf_extras: dict[str, list[str]] = {}
    if naf_individuels_actifs:
        naf_extras["_individuels"] = naf_individuels_actifs

    st.header("Filtres")
    mode = st.radio(
        "Mode",
        options=["premium", "exhaustif"],
        index=0,
        help=(
            "Premium = garde uniquement les producteurs avec un signal de vente directe "
            "(bio, EI, labellisé, petite taille). Recommandé.\n\n"
            "Exhaustif = garde tout (peut sortir 3000+ lignes en zone rurale)."
        ),
    )
    score_min = 1
    if mode == "premium":
        score_min = st.slider("Score min (premium)", 1, 5, 1,
                              help="1 = au moins un signal qualité. Augmente pour ne voir que le top.")
    exclure_grandes = st.checkbox("Exclure ETI/GE", value=True)
    actifs_seulement = st.checkbox("Actifs uniquement", value=True)
    effectif_max = st.slider("Effectif max pour transformateurs (10.xx/11.xx)", 0, 500, 20)

    st.header("Sources")
    st.caption("SIRENE est toujours actif. Les autres sources enrichissent et taguent les labels.")
    src_sirene = st.checkbox("SIRENE (obligatoire)", value=True, disabled=True)
    src_bio = st.checkbox("Agence Bio (national)", value=True)
    src_inao = st.checkbox("INAO AOP/IGP (national)", value=True)
    src_regionales = st.checkbox("Labels régionaux (© du Centre, Saveurs en'Or, etc.)",
                                  value=True,
                                  help="Chargés automatiquement selon les départements demandés "
                                       "depuis les fichiers sources_regions/*.yaml")

    # Lien rapide vers la page d'ajout de source
    st.page_link("pages/1_Ajouter_une_source.py",
                  label="➕ Ajouter / tester une nouvelle source",
                  icon="🔗")

    # Google Maps a été retiré pour rester full gratuit. Enrichissement via Nominatim OSM à venir.


# --------- Bouton de lancement ----------
col_run, col_info = st.columns([1, 3])
with col_run:
    run_clicked = st.button("Lancer le radar", type="primary", width="stretch")
with col_info:
    st.caption(f"Sources actives : SIRENE + labels cochés. Sans clé Google, l'outil tourne quand même.")

if run_clicked:
    # Construire la config en mémoire
    config = {
        "magasin": {
            "nom": nom,
            "adresse": adresse,
            "latitude": None,    # géocodé automatiquement à partir de l'adresse
            "longitude": None,
        },
        "rayon_km": rayon,
        "departements": [d.strip() for d in deps_input.split(",") if d.strip()],
        "categories": cats_actives,
        "filtres": {
            "exclure_grandes_entreprises": exclure_grandes,
            "garder_uniquement_actifs": actifs_seulement,
            "effectif_max_transfo": effectif_max,
            "mode": mode,
            "score_min_premium": score_min,
        },
        "sources": {
            "sirene": True,
            "agence_bio": src_bio,
            "inao": src_inao,
            "regionales": src_regionales,
        },
        "enrichissement_google": False,
        "cache": {"dossier": str(ROOT / "cache"), "ttl_jours": 7},
        "sortie": {"dossier": str(ROOT / "output")},
    }

    # Injecter NAF extras (catégories pré-définies + individuels à la carte)
    naf_effectif = {cat: list(codes) for cat, codes in naf_map.items()}
    for cat, extras in naf_extras.items():
        naf_effectif.setdefault(cat, []).extend(extras)
    # Si on a des NAF individuels et que la catégorie "_individuels" n'est pas dans cats_actives,
    # on l'active pour que le pipeline les prenne en compte.
    if "_individuels" in naf_effectif:
        cats_actives["_individuels"] = True

    st.subheader("Progression du run")
    st.caption("Géocodage + SIRENE (50+ codes NAF × départements) + scraping labels régionaux. "
               "Premier run : 2-4 min. Runs suivants : cache → quelques secondes.")
    log_placeholder = st.empty()
    logs_progression: list[str] = []

    def log_cb(msg: str):
        logs_progression.append(msg)
        # Affiche les 40 dernières lignes — défile en temps réel comme dans PowerShell
        log_placeholder.code("\n".join(logs_progression[-40:]), language="text")

    try:
        df = radar.run(config, naf_effectif, verbose=False, log_cb=log_cb)
    except Exception as e:
        st.error(f"Erreur pendant le run : {e}")
        st.stop()
    # Log final visible
    logs_progression.append(f"=== Run terminé : {len(df)} producteurs ===")
    log_placeholder.code("\n".join(logs_progression[-40:]), language="text")

    st.success(f"{len(df)} producteurs trouvés dans un rayon de {rayon} km.")

    if df.empty:
        st.warning("Aucun producteur trouvé. Vérifie les départements et le rayon.")
        st.stop()

    # Résumé par catégorie
    st.subheader("Résumé par catégorie")
    resume = df.groupby("categorie").agg(
        nb=("nom_complet", "count"),
        dist_moy=("distance_km", "mean"),
    ).round(1).reset_index()
    st.dataframe(resume, width="stretch", hide_index=True)

    # Tableau complet
    st.subheader("Producteurs (triés par catégorie puis distance)")
    # Réordonne les colonnes utiles en tête + rend les URLs cliquables
    cols_tete = [c for c in [
        "nom_complet", "categorie", "distance_km", "commune", "code_postal",
        "labels", "fiche_label", "site_web", "telephone", "fiche_annuaire",
        "score_pertinence", "score_detail", "code_naf", "libelle_naf",
        "dirigeant_principal", "siren",
    ] if c in df.columns]
    autres = [c for c in df.columns if c not in cols_tete]
    df_aff = df[cols_tete + autres]

    col_config = {
        "fiche_label": st.column_config.LinkColumn("Fiche label", display_text="Voir la fiche"),
        "fiche_annuaire": st.column_config.LinkColumn("Annuaire entreprises", display_text="Voir"),
        "site_web": st.column_config.LinkColumn("Site web"),
        "labels": st.column_config.TextColumn("Labels"),
        "score_detail": st.column_config.TextColumn("Détail score", width="medium"),
    }
    st.dataframe(df_aff, width="stretch", hide_index=True, height=400, column_config=col_config)

    # Carte
    st.subheader("Carte")
    carte_path = Path(config["sortie"]["dossier"]) / "carte.html"
    carte_path.parent.mkdir(parents=True, exist_ok=True)
    radar.export_carte(
        df,
        config["magasin"]["latitude"], config["magasin"]["longitude"],
        config["magasin"]["nom"], rayon, str(carte_path),
    )
    with open(carte_path, "r", encoding="utf-8") as f:
        carte_html = f.read()
    # st.components.v1.html : warning de déprécation mais c'est ce qui marche pour Folium.
    st.components.v1.html(carte_html, height=600, scrolling=True)
    st.caption(f"Carte autonome ouvrable directement : `{carte_path}`")

    # Exports téléchargeables
    st.subheader("Téléchargements")
    horodatage = datetime.now().strftime("%Y%m%d_%H%M")
    base = f"producteurs_{nom.replace(' ', '_')}_{horodatage}"

    # Excel en mémoire
    buf = io.BytesIO()
    radar.export_excel(df, buf)  # openpyxl accepte un buffer
    buf.seek(0)
    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button("Excel (.xlsx)", buf.getvalue(),
                           file_name=f"{base}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with c2:
        st.download_button("CSV", df.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"{base}.csv", mime="text/csv")
    with c3:
        with open(carte_path, "r", encoding="utf-8") as f:
            st.download_button("Carte HTML", f.read(),
                               file_name=f"{base}_carte.html", mime="text/html")
else:
    st.info("Configure le magasin et les sources dans le panneau de gauche, puis clique sur **Lancer le radar**.")
