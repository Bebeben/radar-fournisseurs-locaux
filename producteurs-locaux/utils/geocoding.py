"""Geocoding (Nominatim) et calcul de distances."""

import logging
import time

from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

from config import TARGET_LAT, TARGET_LNG

logger = logging.getLogger(__name__)

# Cache en mémoire pour éviter de re-géocoder les mêmes adresses
_geocode_cache: dict[str, tuple[float, float] | None] = {}

_geocoder = Nominatim(user_agent="producteurs-locaux-superu/1.0")


def geocode(address: str) -> tuple[float, float] | None:
    """Géocode une adresse et retourne (lat, lng) ou None."""
    if not address or not address.strip():
        return None

    key = address.strip().lower()
    if key in _geocode_cache:
        return _geocode_cache[key]

    try:
        time.sleep(1.1)  # Nominatim : max 1 req/sec
        location = _geocoder.geocode(address, country_codes="fr", timeout=10)
        if location:
            result = (location.latitude, location.longitude)
            _geocode_cache[key] = result
            return result
        _geocode_cache[key] = None
        return None
    except (GeocoderTimedOut, GeocoderServiceError) as e:
        logger.warning(f"Geocoding échoué pour '{address}': {e}")
        _geocode_cache[key] = None
        return None


def calculate_distance(lat: float, lng: float) -> float:
    """Calcule la distance en km depuis la ville de référence."""
    return round(geodesic((TARGET_LAT, TARGET_LNG), (lat, lng)).km, 1)


def geocode_records(records: list[dict]):
    """Ajoute lat/lng et distance aux enregistrements qui en manquent."""
    count = 0
    for rec in records:
        if rec.get("latitude") and rec.get("longitude"):
            rec["distance_km"] = calculate_distance(rec["latitude"], rec["longitude"])
            continue

        # Tenter le géocodage
        address_parts = [rec.get("ville", ""), rec.get("code_postal", "")]
        address = ", ".join(p for p in address_parts if p)
        if not address:
            rec["distance_km"] = None
            continue

        coords = geocode(address + ", France")
        if coords:
            rec["latitude"], rec["longitude"] = coords
            rec["distance_km"] = calculate_distance(*coords)
            count += 1
        else:
            rec["distance_km"] = None

    if count:
        logger.info(f"Géocodage : {count} adresses résolues")
