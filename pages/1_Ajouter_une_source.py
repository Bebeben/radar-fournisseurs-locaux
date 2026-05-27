"""Page Streamlit pour ajouter / tester un nouveau site label.

Workflow :
1. Tu colles l'URL d'un annuaire de producteurs
2. L'app fetch le HTML et tente le scraper auto
3. Tu vois un aperçu des producteurs détectés
4. Si OK, tu valides et ça écrit dans sources_regions/<region>.yaml
5. Au prochain run du radar, la source est active

Lancement (automatique depuis app.py) : sidebar gauche → "Ajouter une source"
"""
from __future__ import annotations
import streamlit as st
import yaml
from pathlib import Path

# Imports relatifs au projet
import sys
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sources import scraper_generique, regions_loader  # noqa: E402

st.set_page_config(page_title="Ajouter une source", layout="wide")
st.title("Ajouter / Tester une source de producteurs")

st.caption("""
Si tu connais un site qui liste des producteurs locaux (label régional, annuaire d'une
Chambre d'Agriculture, marque collective, etc.), tu peux le tester ici. L'outil tente
d'extraire les producteurs automatiquement. Si le résultat te convient, tu valides
et la source est ajoutée à la base permanente.
""")

# === Saisie ===
col1, col2 = st.columns([2, 1])
with col1:
    url = st.text_input("URL de l'annuaire à tester",
                         placeholder="https://exemple.fr/producteurs")
with col2:
    nom_source = st.text_input("Nom court (slug)", placeholder="ma_marque_locale",
                                help="Sans espaces ni accents. Servira de clé interne et de nom de label.")

description = st.text_input("Description (1 ligne)",
                              placeholder="Marque collective des producteurs du Lubéron — ~120 entreprises")

st.markdown("---")
st.subheader("Configuration avancée (optionnel)")
st.caption("Laisse vide pour utiliser le scraper automatique (heuristiques HTML). "
           "Renseigne ces champs uniquement si l'auto rate son scraping.")

c1, c2, c3 = st.columns(3)
with c1:
    sel_lien = st.text_input("Sélecteur CSS des fiches", placeholder="a.card, article.producteur")
with c2:
    sel_nom = st.text_input("Sélecteur du nom (dans la fiche)", placeholder="h3, .titre")
with c3:
    sel_commune = st.text_input("Sélecteur de la commune", placeholder=".ville, .lieu")

# === Rattachement régional ===
st.markdown("---")
st.subheader("Rattachement géographique")

regions_existantes = regions_loader.charger_toutes_regions()
region_options = ["(nouvelle région)"] + [r.get("region", r.get("_fichier", "?"))
                                             for r in regions_existantes]
region_choisie = st.selectbox("Région cible", region_options)

if region_choisie == "(nouvelle région)":
    nouvelle_region_nom = st.text_input("Nom de la nouvelle région", placeholder="Lubéron, Cantal, ...")
    deps_input = st.text_input("Départements couverts (codes séparés par virgule)",
                                 placeholder="84, 04")
else:
    nouvelle_region_nom = ""
    deps_input = ""

# === Action : tester ===
st.markdown("---")
if st.button("Tester le scraping", type="primary"):
    if not url:
        st.error("Renseigne au moins une URL.")
        st.stop()

    config = {}
    if sel_lien.strip(): config["selecteur_lien"] = sel_lien.strip()
    if sel_nom.strip(): config["selecteur_nom"] = sel_nom.strip()
    if sel_commune.strip(): config["selecteur_commune"] = sel_commune.strip()
    config["regex_commune"] = True

    source_def = {"nom": nom_source or "test", "url": url}
    if config:
        source_def["config"] = config

    with st.spinner("Téléchargement de la page + scraping..."):
        try:
            results = scraper_generique.scrape_source(source_def)
        except Exception as e:
            st.error(f"Erreur de scraping : {e}")
            st.stop()

    if not results:
        st.warning("Aucun producteur détecté. Le scraper auto n'a rien reconnu. "
                   "Essaye de renseigner les sélecteurs CSS manuellement (clic droit > Inspecter sur le site).")
        st.stop()

    st.success(f"{len(results)} producteurs détectés.")
    st.dataframe(results, width="stretch", height=400)

    # Stockage temporaire pour le bouton "valider"
    st.session_state["_dernier_test"] = {
        "source_def": source_def,
        "results": results,
        "description": description,
        "region": region_choisie,
        "nouvelle_region_nom": nouvelle_region_nom,
        "deps_input": deps_input,
    }

# === Action : valider et sauver ===
if "_dernier_test" in st.session_state:
    st.markdown("---")
    st.subheader("Validation finale")
    test = st.session_state["_dernier_test"]
    st.write(f"**Source** : {test['source_def']['nom']} — {len(test['results'])} producteurs détectés")
    st.write(f"**URL** : {test['source_def']['url']}")
    if test['description']:
        st.write(f"**Description** : {test['description']}")

    if st.button("✅ Sauvegarder cette source"):
        # Détermine la région cible
        if test['region'] == "(nouvelle région)":
            if not test['nouvelle_region_nom']:
                st.error("Indique le nom de la nouvelle région.")
                st.stop()
            region_slug = test['nouvelle_region_nom'].lower().replace(" ", "_").replace("-", "_")
            region_nom = test['nouvelle_region_nom']
            deps = [d.strip() for d in test['deps_input'].split(",") if d.strip()]
        else:
            region_nom = test['region']
            region_slug = region_nom.lower().replace(" ", "_").replace("-", "_").replace("'", "")
            # Récupère les départements existants
            r_existante = next((r for r in regions_existantes if r.get("region") == region_nom), None)
            deps = r_existante.get("departements", []) if r_existante else []

        # Compose la source à sauver
        source_a_sauver = {
            "nom": test['source_def']['nom'],
            "description": test['description'] or "",
            "url": test['source_def']['url'],
        }
        if "config" in test['source_def']:
            source_a_sauver["config"] = test['source_def']["config"]

        # Écriture YAML
        yaml_path = ROOT / "sources_regions" / f"{region_slug}.yaml"
        if yaml_path.exists():
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        else:
            data = {"region": region_nom, "departements": deps, "sources": []}
        data.setdefault("sources", []).append(source_a_sauver)
        yaml_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        st.success(f"Source ajoutée à `{yaml_path.relative_to(ROOT)}`. "
                   "Elle sera prise en compte au prochain run du radar.")
        del st.session_state["_dernier_test"]

# === Aperçu des sources existantes ===
st.markdown("---")
st.subheader("Sources actuellement configurées")
for r in regions_existantes:
    with st.expander(f"**{r.get('region', '?')}** — départements : {', '.join(r.get('departements', []))}"):
        for src in r.get("sources", []):
            st.write(f"- **{src.get('nom')}** : {src.get('description', '(pas de description)')}")
            st.caption(f"  URL : {src.get('url', '')}")
