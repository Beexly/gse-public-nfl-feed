import datetime
import json
import pathlib
import re
from zoneinfo import ZoneInfo

from workers.sync_nfl_postgame_to_neon_r2 import (
    in_neon_sleep_window,
    main as worker_main,
    validate_master_dataset,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MASTER_DATASET_PATH = REPO_ROOT / "data" / "2026" / "latest" / "master_dataset.json"
CANONICAL_DATASET_PATH = REPO_ROOT / "dataset" / "gse_2026_nfl_postgame_master_dataset.json"
CSV_DIR = REPO_ROOT / "data" / "2026" / "csv"


def test_in_neon_sleep_window_cst_boundaries():
    chicago = ZoneInfo("America/Chicago")
    # November 23, 2026 is CST (UTC-6)
    t_0229 = datetime.datetime(2026, 11, 23, 2, 29, tzinfo=chicago).astimezone(datetime.timezone.utc)
    t_0230 = datetime.datetime(2026, 11, 23, 2, 30, tzinfo=chicago).astimezone(datetime.timezone.utc)
    t_0629 = datetime.datetime(2026, 11, 23, 6, 29, tzinfo=chicago).astimezone(datetime.timezone.utc)
    t_0630 = datetime.datetime(2026, 11, 23, 6, 30, tzinfo=chicago).astimezone(datetime.timezone.utc)

    assert t_0229.hour == 8 and t_0229.minute == 29
    assert t_0230.hour == 8 and t_0230.minute == 30
    assert t_0629.hour == 12 and t_0629.minute == 29
    assert t_0630.hour == 12 and t_0630.minute == 30

    assert in_neon_sleep_window(t_0229) is False
    assert in_neon_sleep_window(t_0230) is True
    assert in_neon_sleep_window(t_0629) is True
    assert in_neon_sleep_window(t_0630) is False


def test_master_dataset_counts_and_week11_standings():
    data = validate_master_dataset(MASTER_DATASET_PATH)
    assert CANONICAL_DATASET_PATH.read_bytes() == MASTER_DATASET_PATH.read_bytes()
    assert len(list(CSV_DIR.glob("*.csv"))) == 60

    assert data["summary_counts"]["total_tables"] == 60
    assert data["summary_counts"]["total_rows"] == 3705
    assert data["active_nfl_week"] == 11

    tables = data["tables"]
    afc_rows = {row[0]: row for row in tables["pfr_2026_afc_standings_post_wk11_sun"]["rows"]}
    nfc_rows = {row[0]: row for row in tables["pfr_2026_nfc_standings_post_wk11_sun"]["rows"]}

    assert afc_rows["Buffalo Bills*"][2:4] == ["9", "1"]
    assert nfc_rows["San Francisco 49ers*"][2:4] == ["9", "1"]
    assert nfc_rows["Detroit Lions*"][2:4] == ["8", "2"]
    assert nfc_rows["Chicago Bears*"][2:4] == ["8", "2"]

    wk11_rows = tables["pfr_2026_week11_scores_and_mnf"]["rows"]
    assert len(wk11_rows) == 13
    final_games = [r for r in wk11_rows if r[4] == "FINAL"]
    mnf_games = [r for r in wk11_rows if r[4] == "MNF_TONIGHT"]
    assert len(final_games) == 12
    assert len(mnf_games) == 1
    assert mnf_games[0][5] == "Cincinnati Bengals"
    assert mnf_games[0][7] == "Washington Commanders"
    assert mnf_games[0][9] == "CIN (7-2) @ WAS (3-5-1)"


def test_worker_validate_if_unconfigured_exits_zero(monkeypatch):
    for var in (
        "VERCEL_REVALIDATE_URL",
        "VERCEL_REVALIDATE_SECRET",
        "NEON_DATABASE_URL",
        "DATABASE_URL",
        "R2_ENDPOINT_URL",
        "R2_BUCKET",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    assert worker_main(["--validate-if-unconfigured"]) == 0


def test_clean_room_zero_secrets_and_ascii_docs():
    patterns = [
        "sk_" + "live_",
        "sk_" + "test_",
        "ghp_" + r"[A-Za-z0-9]{20,}",
        "gho_" + r"[A-Za-z0-9]{20,}",
        "AKIA" + r"[0-9A-Z]{16}",
        r"postgres(?:ql)?://[^\s\"']+",
        "src/" + "gse_engine",
        "pick" + "_pack",
        "draft" + "kings",
        "fan" + "duel",
        "bet" + "mgm",
        "cae" + "sars",
    ]
    forbidden = re.compile("(" + "|".join(patterns) + ")", re.IGNORECASE)
    skip_dirs = {".git", "__pycache__", ".pytest_cache"}
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or any(part in skip_dirs for part in path.parts):
            continue
        content = path.read_text(encoding="utf-8")
        assert not forbidden.search(content), f"Forbidden token found in {path}"

    for doc_name in ("README.md",):
        doc_text = (REPO_ROOT / doc_name).read_text(encoding="utf-8")
        assert all(ord(ch) <= 127 for ch in doc_text), f"{doc_name} must be pure ASCII"
