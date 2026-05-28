"""API recherche-entreprises (annuaire.entreprise.gouv.fr / api.gouv.fr).
Pas de clé, ~7 req/s. Doc : https://recherche-entreprises.api.gouv.fr/docs/
"""
from __future__ import annotations
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Callable
import requests

BASE = "https://recherche-entreprises.api.gouv.fr/search"
SLEEP = 0.18  # ~5.5 req/s, marge sous le quota
MAX_PAGES_PAR_CODE = 8     # plafond par défaut (transformateurs)
MAX_PAGES_AGRICOLE = 20    # codes 01.xx : plafond plus large car beaucoup de petits producteurs légitimes
N_WORKERS = 4  # parallélisme modéré (4 × 0.18s = 22 req/s en pointe ; quota géré par lock global)

# Lock pour respecter le quota global même en parallèle
_request_lock = threading.Lock()
_last_request_ts = [0.0]


def _throttle():
    """Garantit ~SLEEP secondes entre 2 requêtes à l'échelle globale (tous threads)."""
    with _request_lock:
        elapsed = time.time() - _last_request_ts[0]
        if elapsed < SLEEP:
            time.sleep(SLEEP - elapsed)
        _last_request_ts[0] = time.time()


def _get(params: dict) -> dict:
    """Un appel API avec retry minimal et throttle global."""
    for tentative in range(3):
        try:
            _throttle()
            r = requests.get(BASE, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(1.0)
                continue
        except requests.RequestException:
            time.sleep(0.5)
    return {"results": [], "total_pages": 0, "total_results": 0}


def verifier_siret_actif(siret: str) -> bool | None:
    """Vérifie via l'API si un SIRET correspond à une entreprise active.
    Renvoie True si actif, False si fermé/cessé, None si pas trouvé."""
    if not siret or len(siret) != 14:
        return None
    try:
        _throttle()
        r = requests.get(BASE, params={"q": siret, "per_page": 1}, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        results = data.get("results") or []
        if not results:
            return False  # SIRET pas trouvé = probablement radié
        etat = results[0].get("etat_administratif", "")
        return etat == "A"
    except requests.RequestException:
        return None


_MOTS_GENERIQUES = {
    "ferme", "fermes", "domaine", "maison", "gaec", "earl", "scea", "sarl", "sas", "sasu",
    "sci", "eurl", "ei", "les", "des", "du", "de", "la", "le", "saint", "sainte",
    "apicole", "rucher", "brasserie", "fromagerie", "chevrerie", "exploitation",
}


def _tokens_sig(nom: str) -> set:
    """Tokens significatifs d'un nom (≥4 char, hors mots génériques) pour matcher le nom de famille."""
    import re as _re
    toks = _re.split(r"[\s\-'’]+", (nom or "").lower())
    return {t for t in toks if len(t) >= 4 and t not in _MOTS_GENERIQUES}


def chercher_entreprise_par_nom_commune(nom: str, commune: str = "", code_postal: str = "",
                                        departement: str = "") -> dict | None:
    """Comme chercher_siret_par_nom_commune mais renvoie la fiche SIRENE NORMALISÉE complète
    (siret + code NAF + libellé + dirigeant + coordonnées) au lieu du seul SIRET.
    Permet de catégoriser un orphelin label au lieu de le laisser en 'inconnu'.
    """
    if not nom or len(nom) < 4:
        return None
    try:
        from rapidfuzz import fuzz
    except ImportError:
        fuzz = None

    # Deux requêtes possibles : nom complet, puis tokens distinctifs seuls (nom de famille).
    # Car l'API fait un AND sur les mots : "Maison Apicole Oizon Prissac" → 0 résultat,
    # alors que "Oizon Prissac" → trouve "HERVE OIZON".
    tokens_q_list = sorted(_tokens_sig(nom))
    requetes = []
    if commune:
        requetes.append(f"{nom} {commune}")
        if tokens_q_list:
            requetes.append(f"{' '.join(tokens_q_list)} {commune}")
    else:
        requetes.append(nom)
        if tokens_q_list:
            requetes.append(" ".join(tokens_q_list))

    try:
        results = []
        for q in requetes:
            _throttle()
            params = {"q": q, "per_page": 5, "etat_administratif": "A"}
            if departement:
                params["departement"] = departement
            r = requests.get(BASE, params=params, timeout=10)
            if r.status_code == 200:
                results = (r.json() or {}).get("results") or []
                if results:
                    break
        if not results:
            return None

        nom_lower = nom.lower()
        commune_lower = (commune or "").lower()
        tokens_q = _tokens_sig(nom)
        for r_ in results:
            siege = r_.get("siege") or {}
            nom_sirene = (r_.get("nom_complet") or "").lower()
            commune_sirene = (siege.get("libelle_commune") or "").lower()
            cp_sirene = siege.get("code_postal") or ""
            meme_lieu = False
            if code_postal and cp_sirene == code_postal:
                meme_lieu = True
            elif commune_lower and commune_sirene and commune_lower == commune_sirene:
                meme_lieu = True
            elif code_postal and cp_sirene != code_postal:
                continue
            elif commune_lower and commune_sirene and commune_lower != commune_sirene:
                continue

            # Cas 1 : même lieu + token distinctif commun (ex. "oizon" dans les 2)
            tokens_s = _tokens_sig(nom_sirene)
            if meme_lieu and (tokens_q & tokens_s):
                return extraire_normalise(r_)
            # Cas 2 : nom très ressemblant (fuzzy)
            seuil = 78 if (commune or code_postal) else 88
            if fuzz and fuzz.token_set_ratio(nom_lower, nom_sirene) >= seuil:
                return extraire_normalise(r_)
        return None
    except requests.RequestException:
        return None


def chercher_siret_par_nom_commune(nom: str, commune: str = "", code_postal: str = "",
                                   departement: str = "") -> str | None:
    """Tente de récupérer le SIRET d'un producteur via son nom et sa commune (ou département).

    Renvoie le SIRET (14 chiffres) UNIQUEMENT si :
    - L'API trouve un résultat
    - Le nom du résultat SIRENE est cohérent avec le nom cherché (fuzzy >= 78)
    - La commune / code postal / département correspondent

    Si aucun résultat ne passe ces critères, renvoie None (ne pas inventer un faux SIRET).
    """
    if not nom or len(nom) < 4:
        return None
    try:
        from rapidfuzz import fuzz
    except ImportError:
        fuzz = None

    q = nom + (f" {commune}" if commune else "")
    try:
        _throttle()
        params = {"q": q, "per_page": 5, "etat_administratif": "A"}
        if departement:
            params["departement"] = departement
        r = requests.get(BASE, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        results = data.get("results") or []
        if not results:
            return None

        nom_lower = nom.lower()
        commune_lower = (commune or "").lower()

        for r_ in results:
            siege = r_.get("siege") or {}
            nom_sirene = (r_.get("nom_complet") or "").lower()
            commune_sirene = (siege.get("libelle_commune") or "").lower()
            cp_sirene = siege.get("code_postal") or ""

            # Vérif commune / code postal cohérent (si fournis)
            if code_postal and cp_sirene != code_postal:
                continue
            if commune_lower and commune_sirene and commune_lower != commune_sirene:
                continue

            # Vérif nom : seuil 78 (un peu plus strict). Sans commune (recherche par dpt seul),
            # on exige un meilleur score pour compenser l'absence de filtre géographique fin.
            seuil = 78 if (commune or code_postal) else 88
            if fuzz:
                score = fuzz.token_set_ratio(nom_lower, nom_sirene)
                if score < seuil:
                    continue

            siret = siege.get("siret")
            if siret:
                return siret
        return None
    except requests.RequestException:
        return None


def chercher(code_naf: str, departements: Iterable[str], per_page: int = 25) -> list[dict]:
    """Récupère tous les résultats pour (code_naf, departements). Pagine avec plafond."""
    deps = ",".join(departements)
    resultats = []
    page = 1
    max_pages = MAX_PAGES_AGRICOLE if code_naf.startswith("01.") else MAX_PAGES_PAR_CODE
    while page <= max_pages:
        params = {
            "activite_principale": code_naf,
            "departement": deps,
            "per_page": per_page,
            "page": page,
            "etat_administratif": "A",
        }
        data = _get(params)
        results = data.get("results", []) or []
        resultats.extend(results)
        total_pages = data.get("total_pages", 1)
        if page >= total_pages or not results:
            break
        page += 1
    return resultats


def chercher_multi(codes_naf: list[str], departements: list[str],
                   progress_cb: Callable[[int, int, str, int], None] | None = None) -> list[dict]:
    """Boucle parallèle sur plusieurs codes NAF, dédoublonne par SIREN.

    progress_cb(i, total, code, n_resultats) appelé depuis le THREAD PRINCIPAL
    (via as_completed iteration) — important pour que Streamlit mette à jour son UI.
    """
    vus = set()
    out = []
    total = len(codes_naf)

    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        # Soumet tous les jobs, mémorise quel future correspond à quel code
        futures = {ex.submit(chercher, code, departements): code for code in codes_naf}
        for done_count, fut in enumerate(as_completed(futures), start=1):
            code = futures[fut]
            try:
                results = fut.result()
            except Exception:
                results = []
            n_added = 0
            for r in results:
                siren = r.get("siren")
                if siren and siren not in vus:
                    vus.add(siren)
                    out.append(r)
                    n_added += 1
            # CB depuis le thread principal — Streamlit peut rafraîchir l'UI
            if progress_cb:
                progress_cb(done_count, total, code, n_added)
    return out


def extraire_normalise(r: dict) -> dict:
    """Réduit un résultat API à un dict propre et stable pour le pipeline.

    IMPORTANT : si l'entreprise a `matching_etablissements`, on utilise l'établissement
    matché (qui est dans le département cherché) plutôt que le siège global, qui peut
    être ailleurs en France. Et on exige que cet établissement matché soit ACTIF —
    sinon on flague `etat_etab_local = F` pour que le filtre l'exclue.
    """
    siege = r.get("siege") or {}
    complements = r.get("complements") or {}
    dirigeants = r.get("dirigeants") or []

    # Choix de l'établissement de référence (= établissement dans le dpt cherché)
    matching = r.get("matching_etablissements") or []
    etab_local = None
    etab_local_etat = ""
    if matching:
        # Privilégier un établissement actif s'il y en a un
        actifs = [m for m in matching if m.get("etat_administratif") == "A"]
        if actifs:
            etab_local = actifs[0]
            etab_local_etat = "A"
        else:
            # Aucun actif : on prend le premier, on flaguera comme fermé
            etab_local = matching[0]
            etab_local_etat = etab_local.get("etat_administratif", "F")
    # Si pas de matching ou siège dans le dpt cherché, on garde le siège
    if not etab_local:
        etab_local = siege
        etab_local_etat = siege.get("etat_administratif", "A")
    dirigeant_principal = ""
    if dirigeants:
        d = dirigeants[0]
        nom = d.get("nom") or ""
        prenom = d.get("prenoms") or ""
        dirigeant_principal = f"{prenom} {nom}".strip()
        if not dirigeant_principal:
            dirigeant_principal = d.get("denomination") or ""
    # Site web et téléphone : on cherche dans plusieurs champs possibles
    site_web = (
        r.get("site_internet")
        or complements.get("site_internet")
        or complements.get("site_web")
        or ""
    )
    telephone = (
        r.get("telephone")
        or complements.get("telephone")
        or ""
    )
    email = (
        r.get("email")
        or complements.get("email")
        or ""
    )

    # Lien direct vers la fiche annuaire-entreprises (toujours dispo via SIREN)
    fiche_annuaire = f"https://annuaire-entreprises.data.gouv.fr/entreprise/{r.get('siren', '')}" if r.get("siren") else ""

    return {
        "siren": r.get("siren"),
        "siret": etab_local.get("siret") or siege.get("siret"),
        "nom_complet": r.get("nom_complet") or r.get("nom_raison_sociale") or "",
        "code_naf": r.get("activite_principale") or "",
        "libelle_naf": r.get("libelle_activite_principale") or "",
        "categorie_entreprise": r.get("categorie_entreprise") or "",
        "tranche_effectif": r.get("tranche_effectif_salarie") or "",
        "etat_administratif": r.get("etat_administratif") or "",
        # IMPORTANT : on prend l'adresse/coordonnées de l'établissement LOCAL,
        # pas du siège global. Et on flague si cet établissement local est fermé.
        "etat_etab_local": etab_local_etat,
        "adresse": etab_local.get("adresse") or siege.get("adresse") or "",
        "commune": etab_local.get("libelle_commune") or siege.get("libelle_commune") or "",
        "code_postal": etab_local.get("code_postal") or siege.get("code_postal") or "",
        "latitude": etab_local.get("latitude") or siege.get("latitude"),
        "longitude": etab_local.get("longitude") or siege.get("longitude"),
        "est_bio": bool(complements.get("est_bio")),
        "est_patrimoine_vivant": bool(complements.get("est_patrimoine_vivant")),
        "est_societe_mission": bool(complements.get("est_societe_mission")),
        "est_ess": bool(complements.get("est_ess")),
        "est_entrepreneur_individuel": bool(complements.get("est_entrepreneur_individuel")),
        "dirigeant_principal": dirigeant_principal,
        "site_web": site_web,
        "telephone": telephone,
        "email": email,
        "fiche_annuaire": fiche_annuaire,
    }
