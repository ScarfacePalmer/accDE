import os
import time
import requests
from datetime import datetime, timezone

# Verkehrslage-Karten-API. Große Boxen können Cluster liefern; diese werden
# weiter unterteilt, bis einzelne Unfallmeldungen verfügbar sind.
API_URL = "https://www.verkehrslage.de/api/v1/pois"

CF_API_URL = (
    "https://api.cloudflare.com/client/v4/accounts/"
    "{account_id}/d1/database/{database_id}/query"
)

HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/155.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.verkehrslage.de/unfaelle",
}

# Box-Koordinaten: südliche Breite, westliche Länge, nördliche Breite,
# östliche Länge. Die ersten 14 Boxen stammen aus deiner Liste.
# NRW stammt aus der HAR-Datei. Rheinland-Pfalz fehlte in der Liste und ist
# deshalb mit einer geografischen Umgrenzungsbox ergänzt.
BOXES = [
    ("Baden-Württemberg", (47.20000, 5.80000, 51.16557, 15.10000)),
    ("Bayern", (47.20000, 5.80000, 51.41291, 15.10000)),
    ("Niedersachsen", (52.21266, 12.60132, 52.80110, 14.26300)),
    ("Schleswig-Holstein", (53.07423, 6.47095, 55.10000, 13.11768)),
    ("Sachsen", (49.69606, 10.14038, 52.13349, 15.10000)),
    ("Sachsen-Anhalt", (49.55373, 5.80000, 54.31652, 15.10000)),
    ("Berlin", (52.21266, 12.60132, 52.80110, 14.26300)),
    ("Bremen", (52.97676, 8.32489, 53.26686, 9.15573)),
    ("Saarland", (49.06307, 6.05072, 49.69251, 7.71240)),
    ("Thüringen", (49.69606, 7.94312, 52.13349, 14.58984)),
    ("Mecklenburg-Vorpommern", (52.74959, 9.18457, 55.02802, 15.10000)),
    ("Hessen", (48.01932, 5.80000, 52.93540, 15.10000)),
    ("Brandenburg", (50.05008, 6.35010, 54.76267, 15.10000)),
    ("Hamburg", (53.28492, 9.19556, 53.85901, 10.85724)),
    ("Nordrhein-Westfalen", (50.19097, 5.80000, 52.60305, 11.01929)),
    ("Rheinland-Pfalz", (48.96600, 6.11200, 50.94000, 8.51000)),
]

MAX_CLUSTER_DEPTH = 8
MAX_BOX_REQUESTS = 1500
REQUEST_PAUSE_SECONDS = 0.25
MAX_HTTP_RETRIES = 4
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_session = requests.Session()
_raw_box_cache = {}
_resolved_box_cache = {}
_request_count = 0
_cluster_count_seen = 0


def now():
    return datetime.now(timezone.utc).isoformat()


def box_key(box):
    return tuple(round(float(value), 6) for value in box)


def is_cluster(feature):
    properties = feature.get("properties") or {}
    return (
        properties.get("icon") == "cluster"
        or properties.get("type") == 1000
        or properties.get("clusterCount") is not None
    )


def is_accident(feature):
    properties = feature.get("properties") or {}
    feature_id = feature.get("id")

    if not feature_id or is_cluster(feature):
        return False

    # Aktuelle Antwortstruktur der Verkehrslage-API.
    if (
        str(properties.get("typeSlug", "")).lower() == "unfall"
        and properties.get("icon") == "accident"
    ):
        return True

    # Fallback auf Typ 21, falls typeSlug/icon künftig fehlen.
    return properties.get("type") == 21


def fetch_box_features(box):
