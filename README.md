# gse-public-nfl-feed

Clean-room public MIT-licensed NFL post-game standings, boxscores, and team/player statistical feeds.

## Scope and Clean-Room Boundaries

This repository contains public post-game NFL statistical tables, bitemporal SQL schema definitions, and a lightweight validation/sync worker.

It intentionally contains **zero** proprietary prediction models, **zero** paid pick packs, **zero** sportsbook odds feeds, and **zero** API keys or credentials.

## Repository Layout

- `data/2026/csv/*.csv` - 60 post-game CSV tables covering 2025 reference metrics and 2026 season-to-date standings, Week 11 scores, team efficiency, defense, trench, coverage, and player statistics.
- `data/2026/latest/master_dataset.json` - Consolidated 2026 post-game master JSON feed (60 tables, 3,705 rows, active NFL Week 11).
- `dataset/gse_2026_nfl_postgame_master_dataset.json` - Canonical copy of the consolidated master JSON feed.
- `sql/001_gse_nfl_postgame_neon_schema.sql` - Bitemporal PostgreSQL schema for Neon (`gse_nfl_stats`).
- `workers/sync_nfl_postgame_to_neon_r2.py` - Dataset validator and optional Neon / Cloudflare R2 / Vercel ISR sync worker.
- `tests/test_postgame_feed.py` - Automated verification suite for sleep-window enforcement, dataset integrity, and clean-room secret hygiene.
- `.github/workflows/postgame-nfl-sync.yml` - Window-scoped post-game validation and sync workflow.

## Neon 02:30-06:30 America/Chicago Sleep Guard

To avoid unnecessary database compute wakes during overnight hours, `in_neon_sleep_window(now_utc)` enforces a hard zero-DB-wake window from `02:30` through `06:30` `America/Chicago` (`150 <= minutes < 390`).

Both the Python worker (`workers/sync_nfl_postgame_to_neon_r2.py`) and the GitHub Actions cron schedule (`.github/workflows/postgame-nfl-sync.yml`) are scoped strictly outside `02:30-06:30` Central Time (`08:30-12:30 UTC` during CST).

## Local Validation

```bash
python -m pytest -v
python workers/sync_nfl_postgame_to_neon_r2.py --validate-if-unconfigured
```

In public CI or local runs without sync secrets configured, `--validate-if-unconfigured` validates `data/2026/latest/master_dataset.json` (`60` tables, `3,705` rows) and exits `0` cleanly without attempting external network sync.

## License

MIT - see [LICENSE](LICENSE).
