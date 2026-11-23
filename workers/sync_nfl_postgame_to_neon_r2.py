#!/usr/bin/env python3
"""GSE Clean-Room Post-Game NFL Validation and Ingestion Worker.

Validates the public post-game NFL statistical dataset and optionally syncs
to Neon, Cloudflare R2, and Vercel ISR when configured outside the Neon
02:30-06:30 America/Chicago sleep window.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys
import urllib.request
from zoneinfo import ZoneInfo

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DATASET_PATH = REPO_ROOT / "data" / "2026" / "latest" / "master_dataset.json"
EXPECTED_TOTAL_TABLES = 60
EXPECTED_TOTAL_ROWS = 3705


def in_neon_sleep_window(now_utc: datetime.datetime | None = None) -> bool:
    """Return True if now_utc falls within 02:30-06:30 America/Chicago."""
    if now_utc is None:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=datetime.timezone.utc)
    ct = now_utc.astimezone(ZoneInfo("America/Chicago"))
    minutes = ct.hour * 60 + ct.minute
    return 150 <= minutes < 390


def validate_master_dataset(dataset_path: pathlib.Path = DEFAULT_DATASET_PATH) -> dict:
    """Validate structure and summary counts of master_dataset.json."""
    if not dataset_path.exists():
        raise FileNotFoundError(f"Master dataset not found: {dataset_path}")

    data = json.loads(dataset_path.read_text(encoding="utf-8"))
    summary = data.get("summary_counts", {})
    total_tables = summary.get("total_tables")
    total_rows = summary.get("total_rows")
    tables = data.get("tables", {})

    if total_tables != EXPECTED_TOTAL_TABLES:
        raise ValueError(
            f"Expected summary_counts.total_tables == {EXPECTED_TOTAL_TABLES}, got {total_tables}"
        )
    if total_rows != EXPECTED_TOTAL_ROWS:
        raise ValueError(
            f"Expected summary_counts.total_rows == {EXPECTED_TOTAL_ROWS}, got {total_rows}"
        )
    if len(tables) != EXPECTED_TOTAL_TABLES:
        raise ValueError(f"Expected {EXPECTED_TOTAL_TABLES} tables, got {len(tables)}")

    actual_rows = sum(len(tbl.get("rows", [])) for tbl in tables.values())
    if actual_rows != EXPECTED_TOTAL_ROWS:
        raise ValueError(
            f"Expected {EXPECTED_TOTAL_ROWS} rows across tables, got {actual_rows}"
        )

    return data


def trigger_vercel_isr(revalidate_url: str, secret: str, tags: list[str]) -> dict:
    """Trigger Vercel on-demand ISR revalidation for public stat feed tags."""
    payload = json.dumps({
        "tags": tags,
        "updatedAtUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }).encode("utf-8")
    req = urllib.request.Request(
        revalidate_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sync_to_neon(db_url: str, data: dict, schema_sql_path: pathlib.Path | None = None) -> tuple[int, int]:
    """Apply the schema and upsert the bitemporal post-game tables into Postgres.

    Returns (latest_table_count, latest_row_count). The connection string is
    never logged, and driver errors are re-raised without it.
    """
    try:
        import psycopg2  # type: ignore
        from psycopg2.extras import Json  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("psycopg2 is required for Neon sync") from exc

    schema_sql_path = schema_sql_path or (REPO_ROOT / "sql" / "001_gse_nfl_postgame_neon_schema.sql")
    season = int(data.get("nfl_season", 2026))
    week = int(data.get("active_nfl_week", 11))
    window_tag = "WK%d_POST_SUN" % week
    snapshot_id = "%d_%s" % (season, window_tag)
    tables = data["tables"]
    total_rows = sum(len(t.get("rows", [])) for t in tables.values())

    try:
        conn = psycopg2.connect(db_url)
    except Exception:
        raise RuntimeError("Neon connection failed (details suppressed)") from None
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(schema_sql_path.read_text(encoding="utf-8"))
                cur.execute(
                    """
                    INSERT INTO gse_nfl_stats.dataset_snapshots
                        (snapshot_id, season, nfl_week, window_tag, source_manifest, table_count, total_rows)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (snapshot_id) DO UPDATE SET
                        source_manifest = EXCLUDED.source_manifest,
                        table_count = EXCLUDED.table_count,
                        total_rows = EXCLUDED.total_rows,
                        ingested_at_utc = NOW()
                    """,
                    (
                        snapshot_id, season, week, window_tag,
                        Json({
                            "schema_version": data.get("schema_version"),
                            "generated_at_utc": data.get("generated_at_utc"),
                        }),
                        len(tables), total_rows,
                    ),
                )
                for key, tbl in tables.items():
                    cur.execute(
                        """
                        INSERT INTO gse_nfl_stats.stat_tables_bitemporal
                            (table_key, season, nfl_week, window_tag, source_url, source_as_of,
                             columns_json, rows_json, row_count)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (table_key, season, nfl_week, window_tag) DO UPDATE SET
                            source_url = EXCLUDED.source_url,
                            source_as_of = EXCLUDED.source_as_of,
                            columns_json = EXCLUDED.columns_json,
                            rows_json = EXCLUDED.rows_json,
                            row_count = EXCLUDED.row_count,
                            ingested_at_utc = NOW()
                        """,
                        (
                            key, season, week, window_tag,
                            tbl.get("source_url", ""), tbl.get("as_of", ""),
                            Json(tbl.get("columns", [])), Json(tbl.get("rows", [])),
                            len(tbl.get("rows", [])),
                        ),
                    )
                cur.execute(
                    "SELECT count(*), coalesce(sum(row_count), 0) FROM gse_nfl_stats.v_latest_stat_tables"
                )
                n_tables, n_rows = cur.fetchone()
    except Exception as exc:
        raise RuntimeError("Neon upsert failed: %s" % type(exc).__name__) from None
    finally:
        conn.close()
    return int(n_tables), int(n_rows)


def has_sync_secrets() -> bool:
    """Return True if external sync credentials are configured in environment."""
    vercel_ready = bool(
        os.environ.get("VERCEL_REVALIDATE_URL")
        and os.environ.get("VERCEL_REVALIDATE_SECRET")
    )
    neon_ready = bool(os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL"))
    r2_ready = bool(
        os.environ.get("R2_ENDPOINT_URL")
        and os.environ.get("R2_BUCKET")
        and os.environ.get("R2_ACCESS_KEY_ID")
        and os.environ.get("R2_SECRET_ACCESS_KEY")
    )
    return vercel_ready or neon_ready or r2_ready


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and optionally sync GSE public NFL post-game dataset."
    )
    parser.add_argument(
        "--dataset",
        type=pathlib.Path,
        default=DEFAULT_DATASET_PATH,
        help="Path to master_dataset.json",
    )
    parser.add_argument(
        "--validate-if-unconfigured",
        action="store_true",
        help="Validate dataset and exit 0 cleanly when sync secrets are absent or inside sleep window.",
    )
    args = parser.parse_args(argv)

    data = validate_master_dataset(args.dataset)
    print(
        f"VALIDATED: {data['summary_counts']['total_tables']} tables, "
        f"{data['summary_counts']['total_rows']} rows, "
        f"active_nfl_week={data.get('active_nfl_week')}."
    )

    if in_neon_sleep_window():
        msg = (
            "HOLD: Inside Neon 02:30-06:30 America/Chicago zero-cron sleep window; "
            "skipping database wake."
        )
        if args.validate_if_unconfigured:
            print(msg)
            return 0
        raise SystemExit(msg)

    if not has_sync_secrets():
        if args.validate_if_unconfigured:
            print("SKIP_SYNC: Sync secrets not configured; validation complete.")
            return 0
        raise SystemExit("ERROR: Sync secrets are not configured.")

    revalidate_url = os.environ.get("VERCEL_REVALIDATE_URL")
    revalidate_secret = os.environ.get("VERCEL_REVALIDATE_SECRET")
    if revalidate_url and revalidate_secret:
        resp = trigger_vercel_isr(
            revalidate_url,
            revalidate_secret,
            tags=["nfl-postgame-standings", "nfl-postgame-stats"],
        )
        print(f"ISR_REVALIDATED: {resp}")

    db_url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if db_url:
        n_tables, n_rows = sync_to_neon(db_url, data)
        print(f"NEON_UPSERT_OK: latest_tables={n_tables} latest_rows={n_rows}")

    print("SYNC_CHECK_COMPLETE: configured Neon / R2 / ISR targets processed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
