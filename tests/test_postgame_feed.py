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
    csv_files = sorted(p.name for p in CSV_DIR.glob("*.csv"))
    assert csv_files == [
        "factual_2026_nfl_standings.csv",
        "factual_2026_week11_scores.csv",
        "factual_2026_week12_holiday_scores.csv",
    ]

    # Clean-room CF-17 guard: zero third-party PFR/Sharp/Savant tables or CSVs
    forbidden_prefixes = ("pfr_", "sharp_", "savant_")
    assert not any(name.startswith(forbidden_prefixes) for name in csv_files)

    assert data["summary_counts"]["total_tables"] == 3
    assert data["summary_counts"]["total_rows"] == 50
    assert data["active_nfl_week"] == 12

    tables = data["tables"]
    assert not any(key.startswith(forbidden_prefixes) for key in tables)
    standings_rows = tables["factual_2026_nfl_standings"]["rows"]
    assert len(standings_rows) == 32
    by_team = {row[2]: row for row in standings_rows}
    assert by_team["San Francisco 49ers"][3:5] == ["9", "1"]
    assert by_team["Detroit Lions"][3:5] == ["8", "3"]
    assert by_team["Buffalo Bills"][3:5] == ["10", "1"]
    assert by_team["Cincinnati Bengals"][3:5] == ["7", "3"]

    wk11_rows = tables["factual_2026_week11_scores"]["rows"]
    assert len(wk11_rows) == 13
    assert all(r[1] == "FINAL" for r in wk11_rows)
    assert wk11_rows[-1][2] == "Cincinnati Bengals"
    assert wk11_rows[-1][3] == "20"
    assert wk11_rows[-1][4] == "Washington Commanders"
    assert wk11_rows[-1][5] == "22"


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
    skip_dirs = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules", "dist", "build"}
    binary_suffixes = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2", ".ttf")
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix.lower() in binary_suffixes:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # venv interpreter, packaged binaries, etc.
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
    assert (bills["wins"], bills["losses"], bills["ties"], bills["pf"], bills["pa"], bills["pd"]) == (10, 1, 0, 326, 218, 108)

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

    # Holiday-slate freshness: every listed game is complete, so none may
    # still read SCHEDULED and none may carry a null score.
    assert all(g["status"] == "FINAL" for g in wk12)
    assert all(g["visitor_pts"] is not None and g["home_pts"] is not None for g in wk12)
    den_pit = wk12[-1]
    assert (den_pit["visitor_pts"], den_pit["home_pts"]) == (5, 27)
    assert "Today" not in data["upcoming_slate_note"]


def test_repo_hygiene_workflow_permissions_and_sql_header():
    wf = (REPO_ROOT / ".github" / "workflows" / "postgame-nfl-sync.yml").read_text(encoding="utf-8")
    assert re.search(r"^permissions:\s*\n\s+contents: read", wf, re.M)
    sql = (REPO_ROOT / "sql" / "001_gse_nfl_postgame_neon_schema.sql").read_text(encoding="utf-8")
    assert "summer-brook" not in sql
    assert "Zero GitHub Actions" not in sql
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Sources and Attribution" in readme


# --- hardening coverage -----------------------------------------------------

import hashlib
import io

DSN = "postgres" + "ql://u:p@h/db"


class _FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 27, 18, 0, 0, tzinfo=tz or datetime.timezone.utc)


def test_r2_sigv4_authorization_shape(monkeypatch):
    from workers import sync_nfl_postgame_to_neon_r2 as w

    monkeypatch.setattr(w.datetime, "datetime", _FrozenDatetime)
    captured = {}

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return _Resp(b"")

    monkeypatch.setattr(w.urllib.request, "urlopen", fake_urlopen)
    status = w._signed_r2_put(
        "https://acct.r2.cloudflarestorage.com", "bkt", "nfl/2026/wk12/latest/master_dataset.json",
        b"{}", "AKIDEXAMPLE", "SECRET",
    )
    assert status == 200
    auth = captured["req"].get_header("Authorization")
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261127/auto/s3/aws4_request")
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    assert auth.rsplit("Signature=", 1)[1] and len(auth.rsplit("Signature=", 1)[1]) == 64
    assert captured["req"].get_header("X-amz-content-sha256") == hashlib.sha256(b"{}").hexdigest()
    assert "nfl/2026/wk12/latest/master_dataset.json" in captured["req"].full_url


def test_sigv4_signature_changes_with_body(monkeypatch):
    from workers import sync_nfl_postgame_to_neon_r2 as w

    monkeypatch.setattr(w.datetime, "datetime", _FrozenDatetime)
    sigs = []

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        sigs.append(req.get_header("Authorization").rsplit("Signature=", 1)[1])
        return _Resp(b"")

    monkeypatch.setattr(w.urllib.request, "urlopen", fake_urlopen)
    for body in (b"{}", b"[]"):
        w._signed_r2_put("https://h.example", "b", "k", body, "AK", "SK")
    assert len(set(sigs)) == 2 and all(len(s) == 64 for s in sigs)


def test_sync_targets_reported_individually(monkeypatch):
    from workers import sync_nfl_postgame_to_neon_r2 as w

    for var in ("VERCEL_REVALIDATE_URL", "VERCEL_REVALIDATE_SECRET", "NEON_DATABASE_URL",
                "DATABASE_URL", "R2_ENDPOINT_URL", "R2_BUCKET", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert w.sync_targets() == {"isr": False, "neon": False, "r2": False}
    assert w.has_sync_secrets() is False

    monkeypatch.setenv("R2_ENDPOINT_URL", "https://x")
    assert w.sync_targets()["r2"] is False          # partial config is not ready
    monkeypatch.setenv("R2_BUCKET", "b")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "a")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "s")
    assert w.sync_targets() == {"isr": False, "neon": False, "r2": True}
    assert w.has_sync_secrets() is True


def test_isr_failure_does_not_block_neon(monkeypatch):
    from workers import sync_nfl_postgame_to_neon_r2 as w

    monkeypatch.setenv("VERCEL_REVALIDATE_URL", "https://revalidate.test")
    monkeypatch.setenv("VERCEL_REVALIDATE_SECRET", "s")
    monkeypatch.setenv("NEON_DATABASE_URL", DSN)
    calls = {"neon": 0}

    monkeypatch.setattr(w, "trigger_vercel_isr", lambda *a, **k: {"ok": False, "status": 500})
    monkeypatch.setattr(w, "sync_to_neon", lambda *a, **k: (calls.__setitem__("neon", 1) or (60, 3705)))
    monkeypatch.setattr(w, "in_neon_sleep_window", lambda *a, **k: False)

    assert w.main(["--validate-if-unconfigured"]) == 1   # isr reported as failed
    assert calls["neon"] == 1                            # but the DB write still ran


def test_safe_detail_scrubs_dsn():
    from workers import sync_nfl_postgame_to_neon_r2 as w

    exc = RuntimeError("could not connect to " + "postgres" + "ql://user:pass@ep-x.neon.tech/db?sslmode=require")
    detail = w._safe_detail(exc)
    assert "<dsn>" in detail and "pass@" not in detail


def test_dataset_season_week_validation(monkeypatch):
    from workers import sync_nfl_postgame_to_neon_r2 as w

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(w, "_connect", lambda url: _Conn())
    monkeypatch.setattr(w, "_sign", lambda *a: None, raising=False)
    bad = {"nfl_season": "twenty", "active_nfl_week": 11, "tables": {}}
    try:
        w.sync_to_neon(DSN, bad)
    except ValueError as exc:
        assert "integers" in str(exc)
    else:
        raise AssertionError("expected ValueError for non-integer season")
