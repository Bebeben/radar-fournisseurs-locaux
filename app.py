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

import streamlit as st
import yaml
import pandas as pd

import radar


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

st.set_page_config(page_title="Radar Fournisseurs Locaux", layout="wide", page_icon="🛒")
st.title("Radar Fournisseurs Locaux")
st.caption("Cartographier les producteurs alimentaires autour d'un magasin (Super U).")

config_def = charger_config_defaut()
naf_map = charger_naf()
naf_complet = charger_naf_complet()
villes = charger_villes()

# --------- Sidebar : paramètres magasin ----------
with st.sidebar:
    st.header("Magasin")
    # Saisie libre par défaut. Bouton dépliable pour charger un preset.
    nom = st.text_input("Nom du magasin", "Super U Saint-Benoît-du-Sault")
    adresse = st.text_input("Adresse (sera géocodée automatiquement)",
                             "Saint-Benoît-du-Sault, 36170")
    rayon = st.slider("Rayon (km)", 5, 100, 30)
    deps_input = st.text_input("Départements (codes séparés par virgule)", "36,87,23",
                                help="Met les départements qui couvrent ton rayon, même partiellement.")

    with st.expander("Charger un preset ville"):
        if villes:
            preset = st.selectbox("Preset", ["(aucun)"] + list(villes.keys()))
            if preset != "(aucun)" and st.button("Appliquer ce preset"):
                v = villes[preset]
                st.session_state["_preset_nom"] = v.get("nom", "")
                st.session_state["_preset_adresse"] = v.get("adresse", "")
                st.session_state["_preset_rayon"] = v.get("rayon_km", 30)
                st.session_state["_preset_deps"] = ",".join(v.get("departements", []))
                st.rerun()
        # Récupère les valeurs du preset si on vient de cliquer
        if "_preset_nom" in st.session_state:
            nom = st.session_state.pop("_preset_nom")
            adresse = st.session_state.pop("_preset_adresse")
            rayon = st.session_state.pop("_preset_rayon")
            deps_input = st.session_state.pop("_preset_deps")

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

    status = st.status("Interrogation des sources... (regarde aussi le terminal PowerShell pour le détail)", expanded=True)
    try:
        with status:
            st.write("Géocodage + appels SIRENE (60 codes NAF × départements)... ~1-2 min")
            st.write("Puis scraping labels (Agence Bio, BAF, © du Centre, Brenne, INAO)... ~1-2 min")
            st.write("Au 2e run sur la même config, le cache rend ça quasi-instantané.")
            df = radar.run(config, naf_effectif, verbose=True)
        status.update(label=f"Run terminé : {len(df)} producteurs", state="complete")
    except Exception as e:
        status.update(label="Erreur", state="error")
        st.error(f"Erreur pendant le run : {e}")
        st.stop()

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
    st.dataframe(df, width="stretch", hide_index=True, height=400)

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
