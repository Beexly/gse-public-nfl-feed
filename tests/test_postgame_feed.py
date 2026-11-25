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
    assert data["active_nfl_week"] == 12

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
    assert len(final_games) == 13
    assert len(mnf_games) == 0
    assert final_games[-1][5] == "Cincinnati Bengals"
    assert final_games[-1][6] == "20"
    assert final_games[-1][7] == "Washington Commanders"
    assert final_games[-1][8] == "22"
    assert final_games[-1][9] == "Washington Commanders +2"


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


def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all_keys(v)


def test_factual_nfl_postgame_object_and_governance_cleanup():
    data = validate_master_dataset(MASTER_DATASET_PATH)
    gi = data["governance_invariants"]
    for internal_key in ("vercel_dns_locked", "neon_sleep_window_ct", "operator_sim_hold_untouched"):
        assert internal_key not in gi

    fact = data["factual_nfl_postgame"]
    standings = fact["standings"]
    assert len(standings) == 32
    assert len({t["team"] for t in standings}) == 32
    assert all(not t["team"].endswith(("*", "+")) for t in standings)
    assert {t["conference"] for t in standings} == {"AFC", "NFC"}
    assert len({t["division"] for t in standings}) == 8
    assert all(t["pd"] == t["pf"] - t["pa"] for t in standings)
    bills = next(t for t in standings if t["team"] == "Buffalo Bills")
    assert (bills["wins"], bills["losses"], bills["ties"], bills["pf"], bills["pa"], bills["pd"]) == (9, 1, 0, 313, 208, 105)

    banned = {"srs", "osrs", "dsrs", "sos", "mov"}
    assert not ({k.lower() for k in _all_keys(fact)} & banned)

    wk11 = fact["week11_scores"]
    assert len(wk11) == 13
    assert sum(g["status"] == "FINAL" for g in wk11) == 13
    mnf = [g for g in wk11 if g["day"] == "Mon"]
    assert len(mnf) == 1 and mnf[0]["visitor_pts"] == 20 and mnf[0]["home_pts"] == 22
    assert (mnf[0]["visitor"], mnf[0]["home"]) == ("Cincinnati Bengals", "Washington Commanders")

    cin = next(t for t in standings if t["team"] == "Cincinnati Bengals")
    was = next(t for t in standings if t["team"] == "Washington Commanders")
    assert (cin["wins"], cin["losses"], cin["ties"], cin["pf"], cin["pa"], cin["pd"]) == (7, 3, 0, 298, 215, 83)
    assert (was["wins"], was["losses"], was["ties"], was["pf"], was["pa"], was["pd"]) == (4, 5, 1, 218, 243, -25)

    wk12 = fact["week12_holiday_slate"]
    assert [(g["visitor"], g["home"], g["date"], g["time_et"]) for g in wk12] == [
        ("Green Bay Packers", "Los Angeles Rams", "2026-11-25", "8:15PM"),
        ("Chicago Bears", "Detroit Lions", "2026-11-26", "12:30PM"),
        ("Philadelphia Eagles", "Dallas Cowboys", "2026-11-26", "4:30PM"),
        ("Kansas City Chiefs", "Buffalo Bills", "2026-11-26", "8:20PM"),
        ("Denver Broncos", "Pittsburgh Steelers", "2026-11-27", "3:00PM"),
    ]


def test_repo_hygiene_workflow_permissions_and_sql_header():
    wf = (REPO_ROOT / ".github" / "workflows" / "postgame-nfl-sync.yml").read_text(encoding="utf-8")
    assert re.search(r"^permissions:\s*\n\s+contents: read", wf, re.M)
    sql = (REPO_ROOT / "sql" / "001_gse_nfl_postgame_neon_schema.sql").read_text(encoding="utf-8")
    assert "summer-brook" not in sql
    assert "Zero GitHub Actions" not in sql
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Sources and Attribution" in readme
