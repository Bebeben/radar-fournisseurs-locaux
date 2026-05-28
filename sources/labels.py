"""Sources labels — orchestrateur.

Sources nationales (en Python, API spécifiques) :
- Agence Bio (annuaire opérateurs bio)
- INAO (AOP/IGP)

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


# Helper de géocodage gratuit (api-adresse.data.gouv.fr) avec cache mémoire
_geo_cache: dict = {}


def _geocoder_commune(commune: str, code_postal: str = "") -> tuple[float, float] | None:
    """Géocode une commune via api-adresse (gratuit, sans clé). Renvoie (lat, lon) ou None."""
    if not commune:
        return None
    cache_key = f"{commune}|{code_postal}".lower()
    if cache_key in _geo_cache:
        return _geo_cache[cache_key]
    q = f"{commune} {code_postal}".strip()
    try:
        r = requests.get("https://api-adresse.data.gouv.fr/search/",
                         params={"q": q, "type": "municipality", "limit": 1}, timeout=5)
        feats = (r.json() or {}).get("features") or []
        if feats:
            lon, lat = feats[0]["geometry"]["coordinates"]
            _geo_cache[cache_key] = (lat, lon)
            return (lat, lon)
    except Exception:
        pass
    _geo_cache[cache_key] = None
    return None

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
                    # Département de la source (pour filtrer la recherche SIRET quand pas de commune)
                    deps_src = src.get("departements_specifiques") or []
                    dep_src = deps_src[0] if deps_src else ""
                    enriched = 0
                    geocoded = 0
                    for it in items:
                        if not it.get("siret"):
                            fiche = _sirene.chercher_entreprise_par_nom_commune(
                                it.get("nom", ""), it.get("commune", ""),
                                it.get("code_postal", ""), departement=dep_src,
                            )
                            if fiche and fiche.get("siret"):
                                it["siret"] = fiche["siret"]
                                # Enrichit avec NAF + libellé + dirigeant + coords SIRENE
                                it["code_naf"] = fiche.get("code_naf", "")
                                it["libelle_naf"] = fiche.get("libelle_naf", "")
                                it["dirigeant_principal"] = fiche.get("dirigeant_principal", "")
                                it["siren"] = fiche.get("siren", "")
                                it["site_web"] = fiche.get("site_web", "")
                                it["fiche_annuaire"] = fiche.get("fiche_annuaire", "")
                                if fiche.get("latitude") and fiche.get("longitude"):
                                    it["latitude"] = fiche["latitude"]
                                    it["longitude"] = fiche["longitude"]
                                if not it.get("commune") and fiche.get("commune"):
                                    it["commune"] = fiche["commune"]
                                    it["code_postal"] = fiche.get("code_postal", "")
                                enriched += 1
                        # Géocodage si pas de coordonnées (permet filtrage rayon pour orphelins)
                        if not (it.get("latitude") and it.get("longitude")):
                            lat_lon = _geocoder_commune(it.get("commune", ""), it.get("code_postal", ""))
                            if lat_lon:
                                it["latitude"], it["longitude"] = lat_lon
                                geocoded += 1
                    if verbose:
                        print(f"[region:{nom}] {enriched}/{len(items)} items enrichis SIRET, "
                              f"{geocoded}/{len(items)} géocodés")
                cache_util.save(cache_dossier, cache_key, items)
            except Exception as e:
                if verbose: print(f"[region:{nom}] erreur: {e}")
                items = []
            time.sleep(1.0)
        resultats[nom] = items
    return resultats
