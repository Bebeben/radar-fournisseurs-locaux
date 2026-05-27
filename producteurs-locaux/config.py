"""Configuration centrale pour l'outil de recherche de producteurs locaux."""

import os

# --- Ville de référence ---
TARGET_CITY = "Les Pieux, Manche, France"
TARGET_LAT = 49.5181
TARGET_LNG = -1.8094

# --- Rayon de recherche (km) ---
RADIUS_KM = 50

# --- Clé API Google Places (optionnelle) ---
GOOGLE_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", None)

# --- Concurrents à vérifier ---
COMPETITORS = {
    "Leclerc Equeurdreville": {
        "lat": 49.6392,
        "lng": -1.6536,
    },
    "Intermarché Les Pieux": {
        "lat": 49.5135,
        "lng": -1.8040,
    },
}

# --- Catégories et mots-clés associés ---
CATEGORIES = {
    "Alimentaire": {
        "Fruits et légumes / Maraîchage": [
            "légume", "fruit", "maraîch", "salade", "tomate", "pomme de terre",
            "carotte", "poireau", "chou", "courge", "fraise", "pomme",
        ],
        "Produits laitiers / Crèmerie": [
            "lait", "fromage", "beurre", "crème", "yaourt", "crèmerie",
            "laitier", "camembert", "livarot", "pont-l'évêque",
        ],
        "Viande / Charcuterie": [
            "viande", "boeuf", "porc", "veau", "agneau", "charcuterie",
            "saucisson", "pâté", "boudin", "boucherie", "éleveur", "élevage",
        ],
        "Produits de la mer / Poissonnerie": [
            "huître", "moule", "poisson", "crustacé", "coquillage", "algue",
            "fruits de mer", "pêche", "ostréiculteur", "mytiliculteur",
        ],
        "Boulangerie / Pâtisserie": [
            "pain", "boulange", "pâtisserie", "viennoiserie", "brioche",
            "farine", "meunier",
        ],
        "Boissons": [
            "cidre", "jus", "bière", "calvados", "poiré", "spiritueux",
            "brasserie", "cidrerie", "distillerie", "vin",
        ],
        "Miel / Confitures / Épicerie fine": [
            "miel", "confiture", "épicerie", "apiculteur", "abeille",
            "gelée", "caramel", "sel", "herbes", "aromates",
        ],
        "Oeufs / Volaille": [
            "oeuf", "œuf", "poule", "poulet", "canard", "oie", "volaille",
            "pintade", "dinde",
        ],
        "Conserves / Plats préparés artisanaux": [
            "conserve", "plat préparé", "terrine", "rillette", "soupe",
            "bocaux", "transformé",
        ],
    },
    "Non alimentaire": {
        "Cosmétiques / Savonnerie": [
            "savon", "cosmétique", "crème", "huile essentielle", "shampoing",
            "baume", "savonnerie",
        ],
        "Artisanat": [
            "poterie", "céramique", "bois", "textile", "couture", "tricot",
            "vannerie", "cuir", "bijou", "artisan",
        ],
        "Produits d'entretien écologiques": [
            "entretien", "lessive", "nettoyant", "écologique", "ménage",
        ],
        "Fleurs / Horticulture / Pépinières": [
            "fleur", "plante", "pépinière", "horticult", "jardin",
            "semence", "arbre",
        ],
    },
}

# --- Colonnes du fichier Excel ---
EXCEL_COLUMNS = [
    "Nom du producteur",
    "Raison sociale",
    "Catégorie",
    "Sous-catégorie",
    "Produits phares",
    "Ville",
    "Code postal",
    "Distance (km)",
    "Téléphone",
    "Email",
    "Site web",
    "Réseaux sociaux",
    "Labels / Certifications",
    "Vu chez Leclerc Equeurdreville",
    "Vu chez Intermarché Les Pieux",
    "Source",
    "Date de collecte",
]

# --- Paramètres réseau ---
REQUEST_DELAY = 2  # secondes entre requêtes
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15  # secondes

# --- Fichier de sortie ---
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
OUTPUT_FILE = "prospection_producteurs.xlsx"
