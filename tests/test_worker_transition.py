import pathlib
import re
import shutil
import subprocess

import pytest

WORKER_DIR = pathlib.Path(__file__).resolve().parent.parent / "workers" / "cloudflare-espn-final-dispatcher"
WORKER = WORKER_DIR / "worker.mjs"
WRANGLER = WORKER_DIR / "wrangler.toml"


def test_worker_contains_transition_guard_and_dispatch_contract():
    text = WORKER.read_text(encoding="utf-8")
    assert 'prev[id] === "STATUS_IN_PROGRESS" && cur === "STATUS_FINAL"' in text
    assert "export function detectFinalTransitions" in text
    assert "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard" in text
    assert "https://api.github.com/repos/Beexly/gse-public-nfl-feed/dispatches" in text
    assert '"nfl-game-final"' in text
    assert "env.GITHUB_DISPATCH_TOKEN" in text
    assert "NFL_STATE" in text


def test_worker_and_config_contain_no_token_literal():
    for path in (WORKER, WRANGLER, WORKER_DIR / "transition.test.mjs"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"gh[pousr]_[A-Za-z0-9]{20,}", text), path
        assert "github_pat_" not in text, path
        assert not re.search(r"Bearer\s+[A-Za-z0-9_\-]{20,}", text), path


def test_wrangler_cron_respects_quiet_window_and_min_interval():
    text = WRANGLER.read_text(encoding="utf-8")
    crons = re.search(r"crons\s*=\s*\[(.*?)\]", text, re.S).group(1)
    exprs = re.findall(r'"([^"]+)"', crons)
    assert exprs
    for expr in exprs:
        minute, hour = expr.split()[:2]
        step = re.fullmatch(r"\*/(\d+)", minute)
        assert step and int(step.group(1)) >= 5, f"cron polls more often than every 5 min: {expr}"
        hours = set()
        for part in hour.split(","):
            if "-" in part:
                lo, hi = part.split("-")
                hours.update(range(int(lo), int(hi) + 1))
            else:
                hours.add(int(part))
        assert not (hours & {8, 9, 10, 11, 12}), f"cron overlaps 08:30-12:30 UTC: {expr}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_node_transition_unit_tests():
    res = subprocess.run(
        ["node", "--test", str(WORKER_DIR / "transition.test.mjs")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, res.stdout + res.stderr
