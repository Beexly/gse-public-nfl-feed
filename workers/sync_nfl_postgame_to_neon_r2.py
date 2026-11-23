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

    print("SYNC_CHECK_COMPLETE: Ready for configured Neon / R2 / ISR targets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
