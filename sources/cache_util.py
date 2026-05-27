"""Cache disque simple, TTL en jours. Évite de retaper les API pendant le dev."""
from __future__ import annotations
import hashlib
import json
import time
from pathlib import Path

# Limite de longueur de nom de fichier pour rester sous le MAX_PATH Windows (260 char).
# Au-delà, on garde un préfixe lisible + hash court pour l'unicité.
NAME_MAX = 80


def cache_path(dossier: str, key: str) -> Path:
    p = Path(dossier)
    p.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    if len(safe) > NAME_MAX:
        prefix = safe[:40]
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
        safe = f"{prefix}_{h}"
    return p / f"{safe}.json"


def load(dossier: str, key: str, ttl_jours: int = 7):
    path = cache_path(dossier, key)
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl_jours * 86400:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(dossier: str, key: str, data) -> None:
    path = cache_path(dossier, key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
