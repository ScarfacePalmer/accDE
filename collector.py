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
REQUEST_PAUSE_SECONDS = 0.03

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
    """Liefert die Roh-Features genau einer API-Box, mit einfachem Cache."""
    global _request_count

    key = box_key(box)
    if key in _raw_box_cache:
        return _raw_box_cache[key]

    if _request_count >= MAX_BOX_REQUESTS:
        raise RuntimeError(
            f"Sicherheitsgrenze erreicht: mehr als {MAX_BOX_REQUESTS} "
            "API-Box-Abfragen. D1 wurde noch nicht bereinigt."
        )

    south, west, north, east = key
    params = {
        "box": f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}",
        "types": "Unfall",
    }

    response = _session.get(
        API_URL,
        params=params,
        headers=HEADERS,
        timeout=30,
    )
    _request_count += 1
    time.sleep(REQUEST_PAUSE_SECONDS)
    response.raise_for_status()

    data = response.json()
    if data.get("type") != "FeatureCollection":
        raise RuntimeError(
            f"Ungültige GeoJSON-Antwort für Box {params['box']}"
        )

    features = data.get("features", [])
    if not isinstance(features, list):
        raise RuntimeError(
            f"Ungültige 'features'-Liste für Box {params['box']}"
        )

    _raw_box_cache[key] = features
    return features


def split_box(box):
    """Teilt eine Box in vier angrenzende Quadranten."""
    south, west, north, east = box
    mid_lat = (south + north) / 2.0
    mid_lon = (west + east) / 2.0

    return [
        (south, west, mid_lat, mid_lon),
        (south, mid_lon, mid_lat, east),
        (mid_lat, west, north, mid_lon),
        (mid_lat, mid_lon, north, east),
    ]


def fetch_box_recursive(box, depth=0, label="Bereich"):
    """Löst Cluster durch rekursive Unterteilung der jeweiligen Box auf."""
    global _cluster_count_seen

    key = box_key(box)
    if key in _resolved_box_cache:
        return _resolved_box_cache[key]

    features = fetch_box_features(key)
    accidents = {}
    clusters = []

    for feature in features:
        if is_accident(feature):
            accidents[str(feature["id"])] = feature
        elif is_cluster(feature):
            clusters.append(feature)

    if clusters:
        _cluster_count_seen += len(clusters)
        if depth >= MAX_CLUSTER_DEPTH:
            examples = []
            for cluster in clusters[:3]:
                props = cluster.get("properties") or {}
                coords = (cluster.get("geometry") or {}).get("coordinates")
                examples.append(
                    f"clusterCount={props.get('clusterCount')} coords={coords}"
                )
            raise RuntimeError(
                f"{label}: auch nach {depth} Unterteilungen bleiben "
                f"{len(clusters)} Cluster übrig. Beispiele: "
                + "; ".join(examples)
                + ". Es wurden noch keine D1-Datensätze als beendet markiert."
            )

        for child_box in split_box(key):
            child_features = fetch_box_recursive(
                child_box,
                depth=depth + 1,
                label=label,
            )
            for source_id, feature in child_features.items():
                accidents[source_id] = feature

    _resolved_box_cache[key] = accidents
    return accidents


def get_accidents():
    """Fragt alle vorgegebenen Bereiche ab, löst Cluster auf und dedupliziert."""
    global _request_count, _cluster_count_seen
    _raw_box_cache.clear()
    _resolved_box_cache.clear()
    _request_count = 0
    _cluster_count_seen = 0

    all_features = {}

    # Große Boxen zuerst abfragen. Wenn eine nachweislich vollständig
    # abgefragte Box eine kleinere Box komplett enthält, ist die kleinere
    # Abfrage redundant.
    sorted_boxes = sorted(
        BOXES,
        key=lambda item: (
            (item[1][2] - item[1][0]) * (item[1][3] - item[1][1])
        ),
        reverse=True,
    )

    scanned_boxes = []

    for label, box in sorted_boxes:
        south, west, north, east = box

        contained = any(
            south >= scanned_south
            and west >= scanned_west
            and north <= scanned_north
            and east <= scanned_east
            for scanned_south, scanned_west, scanned_north, scanned_east
            in scanned_boxes
        )

        if contained:
            print(f"{label}: übersprungen (bereits vollständig abgedeckt).")
            continue

        print(f"{label}: frage Bereich ab ...")
        features = fetch_box_recursive(box, depth=0, label=label)
        print(f"{label}: {len(features)} einzelne Unfallmeldungen gefunden.")

        all_features.update(features)
        scanned_boxes.append(box_key(box))

    result = list(all_features.values())

    if not result:
        raise RuntimeError(
            "Keine einzelnen Unfallmeldungen gefunden. "
            "D1 wird aus Sicherheitsgründen nicht verändert."
        )

    print(
        f"Insgesamt {len(result)} eindeutige Unfallmeldungen aus den Bereichen; "
        f"{_request_count} API-Abfragen, {_cluster_count_seen} Cluster erkannt "
        "und aufgelöst."
    )
    return result


def d1_query(sql, params=None):
    account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    database_id = os.environ["CLOUDFLARE_DATABASE_ID"]
    token = os.environ["CLOUDFLARE_API_TOKEN"]

    url = CF_API_URL.format(
        account_id=account_id,
        database_id=database_id,
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "sql": sql,
        "params": params or [],
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("success"):
        raise RuntimeError(f"D1-Error: {result}")

    return result


def d1_results(response):
    """Entnimmt die Result-Zeilen der Cloudflare-D1-API-Antwort."""
    try:
        return response["result"][0]["results"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"Unerwartete D1-Antwort: {response}") from error


def main():
    timestamp = now()

    print("Retrieving data from verkehrslage.de ...")
    features = get_accidents()
    print(f"{len(features)} unique individual accident reports retrieved.")

    current_ids = set()

    # Erst alle Quell-Datensätze validieren und IDs sammeln. D1 wird erst
    # nach erfolgreichem Abschluss der vollständigen Abfrage aktualisiert.
    valid_features = []
    for feature in features:
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        source_id = str(feature.get("id") or "")

        if not source_id or not isinstance(coordinates, list) or len(coordinates) < 2:
            continue

        try:
            longitude = float(coordinates[0])
            latitude = float(coordinates[1])
        except (TypeError, ValueError):
            continue

        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            continue

        current_ids.add(source_id)
        valid_features.append((source_id, latitude, longitude, properties))

    if not valid_features:
        raise RuntimeError(
            "Nach der Validierung sind keine gültigen Unfallmeldungen übrig. "
            "Es wurden keine Datensätze als beendet markiert."
        )

    for source_id, latitude, longitude, properties in valid_features:
        city = properties.get("city")
        street = properties.get("street")
        state = properties.get("state")
        reliability = properties.get("reliability")
        created_at = properties.get("createdAt")
        confirmed_at = properties.get("confirmedAt")

        check = d1_query(
            """
            SELECT id
            FROM accidents
            WHERE source_id = ?
            LIMIT 1
            """,
            [source_id],
        )
        rows = d1_results(check)

        if rows:
            # Auch die Quell-Zeitstempel und sonstigen Eigenschaften aktualisieren.
            d1_query(
                """
                UPDATE accidents
                SET
                    latitude = ?,
                    longitude = ?,
                    city = ?,
                    street = ?,
                    state = ?,
                    reliability = ?,
                    created_at = ?,
                    confirmed_at = ?,
                    last_seen_at = ?,
                    removed_at = NULL
                WHERE source_id = ?
                """,
                [
                    latitude,
                    longitude,
                    city,
                    street,
                    state,
                    reliability,
                    created_at,
                    confirmed_at,
                    timestamp,
                    source_id,
                ],
            )
        else:
            d1_query(
                """
                INSERT INTO accidents (
                    source_id,
                    latitude,
                    longitude,
                    city,
                    street,
                    state,
                    reliability,
                    created_at,
                    confirmed_at,
                    first_seen_at,
                    last_seen_at,
                    removed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                [
                    source_id,
                    latitude,
                    longitude,
                    city,
                    street,
                    state,
                    reliability,
                    created_at,
                    confirmed_at,
                    timestamp,
                    timestamp,
                ],
            )
            print(f"New data: {street} / {city}")

    # Nur dann als beendet markieren, wenn die vollständige Abfrage mit
    # aufgelösten Clustern erfolgreich war.
    result = d1_query(
        """
        SELECT source_id
        FROM accidents
        WHERE removed_at IS NULL
        """
    )

    active_ids = {
        str(row["source_id"])
        for row in d1_results(result)
        if row.get("source_id") is not None
    }

    disappeared = active_ids - current_ids

    for source_id in disappeared:
        d1_query(
            """
            UPDATE accidents
            SET removed_at = ?
            WHERE source_id = ?
              AND removed_at IS NULL
            """,
            [timestamp, source_id],
        )
        print(f"Data no longer available: {source_id}")

    print(
        f"Done. {len(valid_features)} current reports processed; "
        f"{len(disappeared)} closed."
    )


if __name__ == "__main__":
    main()
