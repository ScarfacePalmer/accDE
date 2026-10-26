import os
import requests
from datetime import datetime, timezone

API_URL = (
    "https://www.verkehrslage.de/api/v1/pois"
    "?box=47.20000,5.80000,55.10000,15.10000"
    "&types=Unfall"
)

CF_API_URL = (
    "https://api.cloudflare.com/client/v4/accounts/"
    "{account_id}/d1/database/{database_id}/query"
)

def now():
    return datetime.now(timezone.utc).isoformat()

def get_accidents():
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/155.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.verkehrslage.de/unfaelle",
    }

    r = requests.get(
        API_URL,
        headers=headers,
        timeout=30
    )

    r.raise_for_status()

    data = r.json()

    if data.get("type") != "FeatureCollection":
        raise RuntimeError(
            "Couldn't retrieve GeoJSON FeatureCollection."
        )

    return data.get("features", [])


def d1_query(sql, params=None):

    account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    database_id = os.environ["CLOUDFLARE_DATABASE_ID"]
    token = os.environ["CLOUDFLARE_API_TOKEN"]

    url = CF_API_URL.format(
        account_id=account_id,
        database_id=database_id
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = {
        "sql": sql,
        "params": params or []
    }

    r = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=30
    )

    r.raise_for_status()

    result = r.json()

    if not result.get("success"):
        raise RuntimeError(
            f"D1-Error: {result}"
        )

    return result


def main():

    timestamp = now()

    print("Retrieving data from verkehrslage.de ...")

    features = get_accidents()

    print(
        f"{len(features)} packs retrieved."
    )

    current_ids = set()

    for feature in features:

        properties = feature.get("properties", {})
        geometry = feature.get("geometry", {})

        coordinates = geometry.get("coordinates")

        if not coordinates or len(coordinates) < 2:
            continue

        source_id = str(feature.get("id", ""))

        if not source_id:
            continue

        current_ids.add(source_id)

        longitude = coordinates[0]
        latitude = coordinates[1]

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
            [source_id]
        )

        rows = check["result"][0]["results"]

        if rows:

            d1_query(
                """
                UPDATE accidents
                SET
                    last_seen_at = ?,
                    removed_at = NULL
                WHERE source_id = ?
                """,
                [timestamp, source_id]
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
                    timestamp
                ]
            )

            print(
                f"New data: {street} / {city}"
            )


    result = d1_query(
        """
        SELECT source_id
        FROM accidents
        WHERE removed_at IS NULL
        """
    )

    active_ids = {
        row["source_id"]
        for row in result["result"][0]["results"]
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
            [timestamp, source_id]
        )

        print(
            f"Data no longer available: {source_id}"
        )

    print(
        f"Done. {len(disappeared)} closed."
    )


if __name__ == "__main__":
    main()
