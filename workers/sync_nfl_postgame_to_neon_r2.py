#!/usr/bin/env python3
"""GSE Clean-Room Post-Game NFL Validation and Ingestion Worker.

Validates the public post-game NFL statistical dataset and optionally syncs
to Neon, Cloudflare R2, and Vercel ISR when configured outside the Neon
02:30-06:30 America/Chicago sleep window.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
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


def _http_json(url: str, payload: dict, headers: dict, timeout: int = 20) -> tuple[int, str]:
    """POST JSON and return (status, body). Non-2xx is returned, not raised."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def trigger_vercel_isr(revalidate_url: str, secret: str, tags: list[str]) -> dict:
    """Trigger Vercel on-demand ISR revalidation for public stat feed tags.

    ISR is a cache refresh, not a data write: a failure here must never block the
    database upsert that follows it. Status and body are reported, never raised.
    """
    payload = {
        "tags": tags,
        "updatedAtUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    status, body = _http_json(
        revalidate_url, payload,
        {"Content-Type": "application/json", "Authorization": f"Bearer {secret}"},
    )
    if not 200 <= status < 300:
        return {"ok": False, "status": status, "body": body[:300]}
    try:
        return {"ok": True, "status": status, "json": json.loads(body)}
    except json.JSONDecodeError:
        return {"ok": True, "status": status, "body": body[:300]}

def _safe_detail(exc: Exception, limit: int = 200) -> str:
    """One-line driver/HTTP detail with any connection string scrubbed."""
    text = " ".join(str(exc).split())
    text = re.sub(r"(?:postgres(?:ql)?://|rediss?://)\S+", "<dsn>", text)
    return text[:limit]


def _connect(db_url: str):
    """Connect with a bounded wait and TLS forced when the URL omits sslmode."""
    import psycopg2  # type: ignore

    kwargs: dict[str, object] = {"connect_timeout": 10}
    if "sslmode=" not in db_url:
        kwargs["sslmode"] = "require"
    return psycopg2.connect(db_url, **kwargs)  # type: ignore[arg-type]


def sync_to_neon(db_url: str, data: dict, schema_sql_path: pathlib.Path | None = None) -> tuple[int, int]:
    """Apply the schema and upsert the bitemporal post-game tables into Postgres.

    Returns (latest_table_count, latest_row_count). The connection string is
    never logged, and driver errors are re-raised without it.
    """
    schema_sql_path = schema_sql_path or (REPO_ROOT / "sql" / "001_gse_nfl_postgame_neon_schema.sql")
    try:
        season = int(data.get("nfl_season", 2026))
        week = int(data.get("active_nfl_week", 11))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "nfl_season and active_nfl_week must be integers (got %r / %r)"
            % (data.get("nfl_season"), data.get("active_nfl_week"))
        ) from exc
    if not 1990 <= season <= 2100:
        raise ValueError(f"nfl_season out of range: {season}")
    if not 1 <= week <= 25:
        raise ValueError(f"active_nfl_week out of range: {week}")

    try:
        import psycopg2  # type: ignore
        from psycopg2.extras import Json  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("psycopg2 is required for Neon sync") from exc
    window_tag = "WK%d_POST_SUN" % week
    snapshot_id = "%d_%s" % (season, window_tag)
    tables = data["tables"]
    total_rows = sum(len(t.get("rows", [])) for t in tables.values())

    try:
        conn = _connect(db_url)
    except Exception as exc:  # noqa: BLE001 - driver error surface varies
        raise RuntimeError(
            "Neon connection failed: %s: %s" % (type(exc).__name__, _safe_detail(exc))
        ) from None
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
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Neon upsert failed: %s: %s" % (type(exc).__name__, _safe_detail(exc))
        ) from None
    finally:
        conn.close()
    return int(n_tables), int(n_rows)


def _signed_r2_put(
    endpoint: str,
    bucket: str,
    key: str,
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str = "auto",
    timeout: int = 30,
) -> int:
    """PUT an object to R2 with SigV4 (no SDK, no third-party signing code)."""
    url = "%s/%s/%s" % (endpoint.rstrip("/"), bucket, urllib.parse.quote(key, safe="/"))
    parts = urllib.parse.urlsplit(url)
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_headers = (
        "host:%s\nx-amz-content-sha256:%s\nx-amz-date:%s\n"
        % (parts.netloc, payload_hash, amz_date)
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        ["PUT", parts.path, "", canonical_headers, signed_headers, payload_hash]
    )
    scope = "%s/%s/s3/aws4_request" % (datestamp, region)
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    key_bytes = ("AWS4" + secret_key).encode("utf-8")
    for part in (datestamp, region, "s3", "aws4_request"):
        key_bytes = hmac.new(key_bytes, part.encode("utf-8"), hashlib.sha256).digest()
    signature = hmac.new(key_bytes, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    req = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Content-Type": "application/json",
            "Authorization": (
                "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                % (access_key, scope, signed_headers, signature)
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def sync_to_r2(dataset_path: pathlib.Path, data: dict) -> int:
    """Upload the validated dataset snapshot to R2 under a season/week key."""
    season = int(data.get("nfl_season", 2026))
    week = int(data.get("active_nfl_week", 11))
    key = "nfl/%d/wk%02d/latest/master_dataset.json" % (season, week)
    body = dataset_path.read_bytes()
    return _signed_r2_put(
        os.environ["R2_ENDPOINT_URL"],
        os.environ["R2_BUCKET"],
        key,
        body,
        os.environ["R2_ACCESS_KEY_ID"],
        os.environ["R2_SECRET_ACCESS_KEY"],
    )


def sync_targets() -> dict[str, bool]:
    """Which sync targets are fully configured. Reported individually, never OR-ed."""
    return {
        "isr": bool(
            os.environ.get("VERCEL_REVALIDATE_URL")
            and os.environ.get("VERCEL_REVALIDATE_SECRET")
        ),
        "neon": bool(
            os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
        ),
        "r2": all(
            os.environ.get(var)
            for var in (
                "R2_ENDPOINT_URL",
                "R2_BUCKET",
                "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY",
            )
        ),
    }


def has_sync_secrets() -> bool:
    """Return True if any external sync credential set is configured."""
    return any(sync_targets().values())


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
        print(msg)
        return 0 if args.validate_if_unconfigured else 2

    targets = sync_targets()
    if not any(targets.values()):
        if args.validate_if_unconfigured:
            print("SKIP_SYNC: Sync secrets not configured; validation complete.")
            return 0
        print("ERROR: Sync secrets are not configured.")
        return 3

    print("TARGETS: " + ", ".join(f"{k}={v}" for k, v in targets.items()))

    failed: list[str] = []

    # ISR first, but a failure here must not stop the data path.
    revalidate_url = os.environ.get("VERCEL_REVALIDATE_URL")
    revalidate_secret = os.environ.get("VERCEL_REVALIDATE_SECRET")
    if targets["isr"]:
        assert revalidate_url and revalidate_secret
        try:
            resp = trigger_vercel_isr(
                revalidate_url,
                revalidate_secret,
                tags=["nfl-postgame-standings", "nfl-postgame-stats"],
            )
            print(("ISR_REVALIDATED: %s" if resp.get("ok") else "ISR_FAILED: %s") % json.dumps(resp))
            if not resp.get("ok"):
                failed.append("isr")
        except Exception as exc:  # noqa: BLE001 - never block the DB write on a cache miss
            print("ISR_FAILED: %s: %s" % (type(exc).__name__, _safe_detail(exc)))
            failed.append("isr")

    db_url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if targets["neon"]:
        assert db_url
        try:
            n_tables, n_rows = sync_to_neon(db_url, data)
            print(f"NEON_UPSERT_OK: latest_tables={n_tables} latest_rows={n_rows}")
        except Exception as exc:  # noqa: BLE001
            print("NEON_FAILED: %s" % exc)
            failed.append("neon")

    if targets["r2"]:
        try:
            status = sync_to_r2(args.dataset, data)
            print(f"R2_UPLOAD_OK: http={status}")
        except Exception as exc:  # noqa: BLE001
            print("R2_FAILED: %s: %s" % (type(exc).__name__, _safe_detail(exc)))
            failed.append("r2")

    print(f"SYNC_CHECK_COMPLETE: targets={len([t for t in targets.values() if t])} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
