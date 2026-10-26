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
                updated_count += 1
            else:
                unchanged_count += 1
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
            inserted_count += 1
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
        f"{inserted_count} inserted, {updated_count} updated, "
        f"{unchanged_count} unchanged (no D1 write), "
        f"{len(disappeared)} closed."
    )


if __name__ == "__main__":
    main()
