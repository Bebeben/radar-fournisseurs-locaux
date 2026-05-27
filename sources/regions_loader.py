"""Charge les définitions de labels régionaux depuis sources_regions/*.yaml.

Structure attendue d'un YAML régional :
    region: "Centre-Val de Loire"
    departements: ["18", "28", "36", "37", "41", "45"]
    sources:
      - nom: "cducentre"
        description: "© du Centre — marque régionale Centre-Val de Loire"
        url: "https://www.cducentre.com/liste-adherents/"
        config:
          selecteur_lien: "a[href*='/adherents/']"
          regex_commune: true

Le pipeline radar.py charge tous les YAMLs et, pour la requête d'un magasin,
ne déclenche que les sources dont les départements croisent ceux du magasin.
"""
from __future__ import annotations
from pathlib import Path
import yaml

DEFAULT_DIR = Path(__file__).parent.parent / "sources_regions"


def charger_toutes_regions(dossier: Path | str = DEFAULT_DIR) -> list[dict]:
    """Charge tous les fichiers YAML de sources régionales."""
    d = Path(dossier)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            if data:
                data["_fichier"] = f.name
                out.append(data)
        except Exception:
            continue
    return out


def sources_pertinentes(regions: list[dict], departements_magasin: list[str]) -> list[dict]:
    """Renvoie les définitions de sources dont les départements croisent ceux du magasin.

    Une source peut avoir un champ `departements_specifiques` qui restreint son
    activation à un sous-ensemble. Si absent, elle s'active dès que la région est concernée.
    """
    deps = set(departements_magasin)
    out = []
    for r in regions:
        deps_region = set(r.get("departements", []))
        if not deps_region or deps & deps_region:
            for src in r.get("sources", []):
                # Filtrage département-spécifique (ex. marque départementale type Is(H)ere)
                dep_spec = set(src.get("departements_specifiques", []) or [])
                if dep_spec and not (deps & dep_spec):
                    continue
                src["_region"] = r.get("region", "")
                out.append(src)
    return out


def ajouter_source_yaml(region_slug: str, source_def: dict, dossier: Path | str = DEFAULT_DIR) -> Path:
    """Ajoute une nouvelle source à un fichier régional (le crée si besoin).
    Retourne le chemin du fichier modifié.
    """
    d = Path(dossier)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{region_slug}.yaml"
    if f.exists():
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    else:
        data = {"region": region_slug, "departements": [], "sources": []}
    data.setdefault("sources", []).append(source_def)
    f.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return f
