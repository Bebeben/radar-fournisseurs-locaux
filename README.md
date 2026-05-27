# Radar Fournisseurs Locaux

Cartographie les producteurs alimentaires autour d'un magasin (Super U), en croisant :
- **SIRENE** (API recherche-entreprises, open data, sans clé) — colonne vertébrale
- **Agence Bio** (annuaire officiel opérateurs bio)
- **Bienvenue à la Ferme** (réseau Chambres d'Agriculture)
- **© du Centre** (marque régionale Centre-Val de Loire)
- **Marque Parc Naturel de la Brenne**
- **INAO** (AOP/IGP)
- (Optionnel) **Google Maps Places** pour téléphone + site + avis

## Installation

```bash
pip install -r requirements.txt
```

## Lancement

### Interface web (recommandé)

```bash
streamlit run app.py
```

Ouvre `http://localhost:8501` dans ton navigateur. Sélectionne un preset ville (Saint-Benoît-du-Sault, Les Pieux, Vaucresson) ou saisis l'adresse à la main. Coche les catégories, ajuste le rayon, clique **Lancer le radar**.

**Pour accès mobile** : déploie sur [Streamlit Community Cloud](https://share.streamlit.io/) (gratuit) en poussant ce repo sur GitHub.

### Ligne de commande

```bash
# Lance avec config par défaut (St-Benoît-du-Sault, 30 km)
python radar.py --config config.yaml

# Bascule sur un autre magasin
python radar.py --ville les_pieux
python radar.py --ville vaucresson --rayon 25

# Force le re-fetch (ignore le cache)
python radar.py --no-cache
```

Les sorties (xlsx, csv, geojson, carte.html) atterrissent dans `output/`.

## Ajouter un magasin

Éditer `villes.yaml` :

```yaml
mon_nouveau_magasin:
  nom: "Super U Mon-Bourg"
  adresse: "Ville, code postal"
  rayon_km: 30
  departements: ["XX", "YY"]
```

Puis `python radar.py --ville mon_nouveau_magasin`.

## Modifier les codes NAF

Éditer `naf.yaml` — une catégorie = une liste de codes APE. Un changement persistant pour tous les futurs runs.

Pour un ajout one-shot (sans toucher au yaml), passer par l'interface Streamlit : champ "NAF extra" dans la sidebar.

## Activer Google Maps Places

```bash
export GOOGLE_MAPS_API_KEY="ta_clé"     # Mac/Linux
$env:GOOGLE_MAPS_API_KEY="ta_clé"       # PowerShell Windows
```

Puis `enrichissement_google: true` dans `config.yaml` ou case cochée dans Streamlit.

## Limites à assumer

- Le code NAF ne distingue pas toujours l'artisan du semi-industriel → filtrage par taille, jamais parfait.
- Le **siège** ≠ toujours le lieu de production (on géolocalise le siège).
- L'outil rate les producteurs sans présence SIRENE/label exploitable.
- Aucune info sur volumes / régularité / capacité à livrer un Super U / intention de bosser avec : **ça reste du terrain**.
- Les scrapers labels (BAF, © du Centre, Brenne) reposent sur des structures HTML qui peuvent évoluer. En cas de zéro résultat sur une source, vérifier les sélecteurs CSS dans `sources/labels.py`.
- L'enrichissement Google a un coût au-delà du quota gratuit.

## Architecture

```
radar.py              # core : pipeline complet
app.py                # interface Streamlit
config.yaml           # paramètres par défaut
naf.yaml              # mapping catégorie → codes NAF
villes.yaml           # presets de magasins
sources/
  sirene.py           # API recherche-entreprises
  labels.py           # Agence Bio, BAF, © du Centre, Brenne, INAO
  google_places.py    # enrichissement Google (optionnel)
  cache_util.py       # cache disque (TTL 7 jours)
cache/                # données mises en cache (JSON)
output/               # exports xlsx/csv/geojson + carte.html
```
