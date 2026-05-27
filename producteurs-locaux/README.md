# Recherche de Producteurs Locaux — Super U

Outil de prospection qui recherche les producteurs, artisans et fournisseurs locaux autour d'une ville et genere un fichier Excel de prospection.

## Installation

```bash
cd producteurs-locaux
pip install -r requirements.txt
```

## Utilisation

### Lancement basique (Les Pieux, rayon 50km)

```bash
python main.py
```

### Options

```bash
# Changer la ville et le rayon
python main.py --city "Cherbourg, Manche, France" --radius 30

# Avec Google Places API (resultats plus riches)
python main.py --google-api-key VOTRE_CLE_API

# Ou via variable d'environnement
export GOOGLE_PLACES_API_KEY=VOTRE_CLE_API
python main.py

# Selectionner certaines sources uniquement
python main.py --sources bienvenue,locavor

# Specifier le fichier de sortie
python main.py --output mon_fichier.xlsx
```

### Sources disponibles

| Source | Identifiant CLI | Cle API |
|--------|----------------|---------|
| Bienvenue a la Ferme | `bienvenue` | Non |
| Google Places | `google` | Oui (optionnel) |
| Pages Jaunes | `pagesjaunes` | Non (best-effort) |
| Acheter a la Source | `acheteralasource` | Non |
| Locavor | `locavor` | Non |

## Google Places API

Pour obtenir les meilleurs resultats, configurez une cle Google Places API :

1. Allez sur [Google Cloud Console](https://console.cloud.google.com/)
2. Activez l'API "Places API (New)"
3. Creez une cle API
4. Lancez : `python main.py --google-api-key VOTRE_CLE`

Sans cle, l'outil fonctionne avec les autres sources (mode degrade).

## Fichier de sortie

Le fichier Excel `output/prospection_producteurs.xlsx` contient 17 colonnes :

- Nom, raison sociale, categorie, sous-categorie, produits
- Ville, code postal, distance depuis le magasin
- Telephone, email, site web, reseaux sociaux
- Labels / certifications (Bio, AOP, IGP...)
- Presence chez Leclerc Equeurdreville (Oui / Non / Inconnu)
- Presence chez Intermarche Les Pieux (Oui / Non / Inconnu)
- Source de l'information, date de collecte

### Formatage

- En-tetes en gras avec filtres automatiques
- Tri par distance (du plus proche au plus eloigne)
- Mise en forme conditionnelle sur les colonnes concurrentielles
- Liens cliquables sur les sites web

## Relancement

L'outil est idempotent : relancez-le pour enrichir le fichier existant sans creer de doublons.

```bash
# Premiere execution
python main.py

# Relancement (fusionne avec l'existant)
python main.py
```

## Configuration

Modifiez `config.py` pour :

- Changer la ville de reference et les coordonnees GPS
- Modifier le rayon de recherche
- Ajouter/retirer des concurrents
- Ajuster les categories et mots-cles
- Modifier les delais entre requetes

## Structure du projet

```
producteurs-locaux/
├── main.py              # Point d'entree + export Excel
├── config.py            # Parametres
├── scrapers/            # Un module par source
│   ├── base.py          # Interface abstraite
│   ├── bienvenue_ferme.py
│   ├── google_places.py
│   ├── pages_jaunes.py
│   ├── acheteralasource.py
│   └── locavor.py
├── enrichment/
│   └── competitor_checker.py
├── utils/
│   ├── geocoding.py     # Geocodage + distances
│   ├── categorizer.py   # Classification automatique
│   └── dedup.py         # Dedoublonnage
├── output/              # Fichier Excel genere
└── requirements.txt
```
