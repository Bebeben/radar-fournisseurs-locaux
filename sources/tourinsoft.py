"""Enrichissement des sources Tourinsoft (offices de tourisme : Visit Limousin, etc.).

Ces sites ont une navigation JavaScript : la liste donne le nom + un `data-sheet-id`,
mais pas la commune ni le lien direct vers la fiche.

Stratégie :
1. Charger le sitemap XML du site → mapping {sheet_id: url_fiche}
   (les URLs ont le format .../slug-commune-fr-<ID>/)
2. Pour chaque producteur (avec son sheet_id), retrouver l'URL de sa fiche
3. Crawler la fiche → extraire le code postal + commune (ex. "87800 La Roche-l'Abeille")

Résultat : on remplit commune, code_postal, url_fiche pour chaque item, ce qui permet
de le géocoder et de le filtrer par rayon.
"""
from __future__ import annotations
import re
import time
import requests
from . import cache_util

UA = {"User-Agent": "RadarFournisseursLocaux/1.0 (associé Super U)"}
RE_CP_COMMUNE = re.compile(r"\b(\d{5})\s+([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\-'’\s]{2,45})")


def charger_mapping_sitemap(sitemap_base: str, n_sitemaps: int, cache_dossier: str,
                            ttl: int = 7) -> dict:
    """Construit {sheet_id: url_fiche} à partir des sous-sitemaps.
    sitemap_base contient {n} qui sera remplacé par 1..n_sitemaps.
    """
    key = "tourinsoft_sitemap_" + re.sub(r"\W+", "_", sitemap_base)
    cached = cache_util.load(cache_dossier, key, ttl)
    if cached is not None:
        return cached

    mapping = {}
    for i in range(1, n_sitemaps + 1):
        url = sitemap_base.replace("{n}", str(i))
        try:
            r = requests.get(url, headers=UA, timeout=15)
            if r.status_code != 200:
                continue
            for m in re.finditer(r"(https://[^<\s]+?-fr-(\d+))/?", r.text):
                mapping[m.group(2)] = m.group(1)
        except requests.RequestException:
            continue
        time.sleep(0.3)
    cache_util.save(cache_dossier, key, mapping)
    return mapping


def extraire_cp_commune_fiche(url_fiche: str) -> tuple[str, str]:
    """Crawl une fiche producteur et extrait (commune, code_postal)."""
    try:
        r = requests.get(url_fiche, headers=UA, timeout=10)
        if r.status_code != 200:
            return "", ""
        html = r.text
        m = RE_CP_COMMUNE.search(html)
        if m:
            cp = m.group(1)
            commune = re.sub(r"\s+", " ", m.group(2)).strip()
            # Coupe la commune à un éventuel mot parasite après (garde max 4 tokens)
            tokens = commune.split()
            commune = " ".join(tokens[:5])
            return commune, cp
    except requests.RequestException:
        pass
    return "", ""


def enrichir_items(items: list[dict], config: dict, cache_dossier: str,
                   ttl: int = 7, verbose: bool = False) -> int:
    """Enrichit les items d'une source Tourinsoft : remplit commune + code_postal + url_fiche
    via le sitemap (ID→URL) puis crawl de chaque fiche. Renvoie le nombre d'items enrichis.

    config attendu :
      sitemap_base : "https://.../sitemap-{n}.xml"
      sitemap_count : 9
    """
    sitemap_base = config.get("sitemap_base")
    n_sitemaps = config.get("sitemap_count", 1)
    if not sitemap_base:
        return 0

    mapping = charger_mapping_sitemap(sitemap_base, n_sitemaps, cache_dossier, ttl)
    if verbose:
        print(f"[tourinsoft] {len(mapping)} fiches indexées via sitemap")

    # Cache des fiches crawlées (commune/cp par url) pour éviter de recrawler
    fiche_cache_key = "tourinsoft_fiches_" + re.sub(r"\W+", "_", sitemap_base)
    fiches_cache = cache_util.load(cache_dossier, fiche_cache_key, ttl) or {}

    n_enrichis = 0
    for it in items:
        if it.get("commune"):
            continue
        sid = it.get("_sheet_id")
        if not sid:
            continue
        url = mapping.get(str(sid))
        if not url:
            continue
        it["url_fiche"] = url
        # Récupère commune/cp depuis le cache fiche, sinon crawl
        if url in fiches_cache:
            commune, cp = fiches_cache[url]
        else:
            commune, cp = extraire_cp_commune_fiche(url)
            fiches_cache[url] = [commune, cp]
            time.sleep(0.3)  # politesse
        if commune:
            it["commune"] = commune
            it["code_postal"] = cp
            n_enrichis += 1

    cache_util.save(cache_dossier, fiche_cache_key, fiches_cache)
    if verbose:
        print(f"[tourinsoft] {n_enrichis}/{len(items)} items enrichis (commune via fiche)")
    return n_enrichis
