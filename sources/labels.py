"""Sources labels — orchestrateur.

Sources nationales (en Python, API spécifiques) :
- Agence Bio (annuaire opérateurs bio)
- INAO (AOP/IGP)
- Marchés des Producteurs de Pays (national, Chambres Agri)

Sources régionales (déclaratives, fichiers sources_regions/*.yaml) :
- © du Centre, Saveurs en'Or, Produit en Bretagne, Sud de France, etc.
- Marque Parc des PNR
- Tout ce qu'on ajoute via l'UI

Chaque source renvoie une liste de dicts {nom, commune, code_postal, lat, lon, label}.
"""
from __future__ import annotations
import time
import requests
from . import cache_util
from . import scraper_generique
from . import regions_loader

UA = {"User-Agent": "RadarFournisseursLocaux/1.0 (associé Super U)"}


# ====================================================================
# Source nationale 1 — Agence Bio
# ====================================================================

AGENCE_BIO_URL = "https://opendata.agencebio.org/api/gouv/operateurs"


def agence_bio(departements: list[str], cache_dossier: str, ttl: int = 7) -> list[dict]:
    """Liste des opérateurs bio certifiés par département."""
    key = f"agence_bio_{'_'.join(sorted(departements))}"
    cached = cache_util.load(cache_dossier, key, ttl)
    if cached is not None:
        return cached

    out = []
    for dep in departements:
        page = 1
        while True:
            try:
                r = requests.get(
                    AGENCE_BIO_URL,
                    params={"departement": dep, "page": page, "size": 100},
                    headers=UA, timeout=10,
                )
                if r.status_code != 200:
                    break
                data = r.json()
            except Exception:
                break

            items = data.get("items") or data.get("data") or []
            if not items:
                break
            for it in items:
                adr = it.get("adressesOperateurs", [{}])
                if isinstance(adr, list) and adr:
                    a = adr[0]
                else:
                    a = it
                out.append({
                    "nom": it.get("denominationcourante") or it.get("raisonSociale") or "",
                    "commune": a.get("ville") or it.get("ville") or "",
                    "code_postal": str(a.get("codePostal") or it.get("codePostal") or ""),
                    "latitude": a.get("lat") or it.get("lat"),
                    "longitude": a.get("long") or it.get("long"),
                    "siret": it.get("siret") or "",
                    "label": "agence_bio",
                })
            total_pages = data.get("totalPages") or 1
            if page >= total_pages:
                break
            page += 1
            time.sleep(0.3)
    cache_util.save(cache_dossier, key, out)
    return out


# ====================================================================
# Source nationale 2 — INAO AOP/IGP (placeholder, à compléter)
# ====================================================================

def inao(cache_dossier: str, ttl: int = 30) -> list[dict]:
    """Placeholder INAO — à compléter avec le bon CSV data.gouv."""
    key = "inao_operateurs"
    cached = cache_util.load(cache_dossier, key, ttl)
    if cached is not None:
        return cached
    out = []
    cache_util.save(cache_dossier, key, out)
    return out


# ====================================================================
# Sources régionales — déclaratives via sources_regions/*.yaml
# ====================================================================

def sources_regionales(departements_magasin: list[str], cache_dossier: str,
                       ttl: int = 7, verbose: bool = False,
                       enrichir_siret: bool = True) -> dict[str, list[dict]]:
    """Charge toutes les sources régionales pertinentes pour les départements du magasin.
    Renvoie un dict {nom_source: [items]}.

    Si enrichir_siret=True, pour chaque item sans SIRET on tente une recherche SIRENE
    par (nom, commune) pour récupérer le SIRET officiel. Ça rend le matching ultérieur
    100% fiable au lieu d'un fuzzy potentiellement faux.
    """
    from . import sirene as _sirene
    regions = regions_loader.charger_toutes_regions()
    sources = regions_loader.sources_pertinentes(regions, departements_magasin)
    resultats = {}
    for src in sources:
        nom = src.get("nom", "inconnu")
        cache_key = f"region_{nom}"
        cached = cache_util.load(cache_dossier, cache_key, ttl)
        if cached is not None:
            items = cached
            if verbose: print(f"[region:{nom}] cache hit ({len(items)} items)")
        else:
            try:
                items = scraper_generique.scrape_source(src)
                if verbose: print(f"[region:{nom}] {len(items)} items scrapés (pré-enrichissement)")
                if enrichir_siret and items:
                    # Enrichit chaque item avec son SIRET via une recherche SIRENE
                    enriched = 0
                    for it in items:
                        if it.get("siret"):
                            continue
                        siret = _sirene.chercher_siret_par_nom_commune(
                            it.get("nom", ""), it.get("commune", ""), it.get("code_postal", "")
                        )
                        if siret:
                            it["siret"] = siret
                            enriched += 1
                    if verbose: print(f"[region:{nom}] {enriched}/{len(items)} items enrichis avec SIRET")
                cache_util.save(cache_dossier, cache_key, items)
            except Exception as e:
                if verbose: print(f"[region:{nom}] erreur: {e}")
                items = []
            time.sleep(1.0)
        resultats[nom] = items
    return resultats
