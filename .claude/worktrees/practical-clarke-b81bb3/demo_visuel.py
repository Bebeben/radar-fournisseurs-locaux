"""
demo_visuel
===========

POC du visuel relevé concurrence avec données fictives.
Wrappe directement le module de prod ``src.visuel_concurrence`` pour
garantir que ce que Benjamin voit ici = ce qu'il recevra en prod.

Lancer : ``python demo_visuel.py`` → génère ``demo_releve_concurrence.png``.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from src.visuel_concurrence import generer_png_concurrence


# Données fictives réalistes
NOS_PRIX = {"Gazole": 1.950, "SP95-E10": 1.705, "SP98": 1.820, "E85": 0.799}

CONCURRENTS = {
    "Intermarché Les Pieux": {
        "Gazole": 1.945, "SP95-E10": 1.695, "SP98": None, "E85": 0.789,
        "derniere_maj_api": "07/05 12:08",
    },
    "Super U Bricquebec": {
        "Gazole": 1.955, "SP95-E10": 1.710, "SP98": None, "E85": None,
        "derniere_maj_api": "06/05 17:30",
    },
}

# Bricquebec : alignement partiel (Gazole + SP95-E10 seulement)
ALIGNEMENTS = {"Super U Bricquebec": ["Gazole", "SP95-E10"]}

if __name__ == "__main__":
    maintenant = datetime(2026, 5, 7, 13, 45, tzinfo=ZoneInfo("Europe/Paris"))
    png = generer_png_concurrence(
        prix_concurrents=CONCURRENTS,
        nos_prix=NOS_PRIX,
        nom_nous="Super U Les Pieux",
        marge_ponderee=0.0353,
        marge_cible=0.0350,
        maintenant=maintenant,
        alignements_partiels=ALIGNEMENTS,
    )
    out = "demo_releve_concurrence.png"
    with open(out, "wb") as f:
        f.write(png)
    print(f"Image sauvée : {out}")
