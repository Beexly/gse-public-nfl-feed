# gse-public-nfl-feed

Clean-room public MIT-licensed NFL post-game standings, boxscores, and team/player statistical feeds.

## Scope and Clean-Room Boundaries

This repository contains public post-game NFL statistical tables, bitemporal SQL schema definitions, and a lightweight validation/sync worker.

It intentionally contains **zero** proprietary prediction models, **zero** paid pick packs, **zero** sportsbook odds feeds, and **zero** API keys or credentials.

## Repository Layout

- `data/2026/csv/*.csv` - 3 factual public-domain post-game CSV tables (`factual_2026_nfl_standings.csv`, `factual_2026_week11_scores.csv`, `factual_2026_week12_holiday_scores.csv`).
- `data/2026/latest/master_dataset.json` - Consolidated 2026 post-game master JSON feed (3 factual tables, 50 rows, active NFL Week 12).
- `dataset/gse_2026_nfl_postgame_master_dataset.json` - Canonical copy of the consolidated master JSON feed.
- `sql/001_gse_nfl_postgame_neon_schema.sql` - Bitemporal PostgreSQL schema for Neon (`gse_nfl_stats`).
- `workers/sync_nfl_postgame_to_neon_r2.py` - Dataset validator and optional Neon / Cloudflare R2 / Vercel ISR sync worker.
- `tests/test_postgame_feed.py` - Automated verification suite for sleep-window enforcement, dataset integrity, and clean-room secret hygiene.
- `.github/workflows/postgame-nfl-sync.yml` - Window-scoped post-game validation and sync workflow.

## Neon 02:30-06:30 America/Chicago Sleep Guard

To avoid unnecessary database compute wakes during overnight hours, `in_neon_sleep_window(now_utc)` enforces a hard zero-DB-wake window from `02:30` through `06:30` `America/Chicago` (`150 <= minutes < 390`).

Both the Python worker (`workers/sync_nfl_postgame_to_neon_r2.py`) and the GitHub Actions cron schedule (`.github/workflows/postgame-nfl-sync.yml`) are scoped strictly outside `02:30-06:30` Central Time (`08:30-12:30 UTC` during CST).

## Sync Targets and Exit Codes

Each target is configured and reported independently, so one failing does not silently take the
others down. `ISR` runs first but its failure never blocks the database write; a cache that stays
stale for one cycle is recoverable, a missing data load is not.

| Target | Env vars | Effect |
|---|---|---|
| Vercel ISR | `VERCEL_REVALIDATE_URL`, `VERCEL_REVALIDATE_SECRET` | POST tag revalidation |
| Neon | `NEON_DATABASE_URL` or `DATABASE_URL` | schema apply + upsert in one transaction |
| Cloudflare R2 | `R2_ENDPOINT_URL`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | SigV4 PUT of the dataset to `nfl/<season>/wk<NN>/latest/master_dataset.json` |

Exit codes: `0` all configured targets succeeded (or nothing configured with
`--validate-if-unconfigured`), `1` at least one target failed, `2` inside the sleep window with the
flag absent, `3` no sync secrets configured with the flag absent. Log lines are prefixed per target
(`ISR_`, `NEON_`, `R2_`) and a `TARGETS:` line states what was configured before anything runs.

Note: `window_tag` is currently always `WK<week>_POST_SUN`. Multiple in-week windows (TNF, SNF,
MNF) collapse onto that tag and the `ON CONFLICT` key is `(table_key, season, nfl_week, window_tag)`,
so only the last write for a week survives. Pass a real window tag before relying on intra-week
bitemporality.

## Local Validation

```bash
python -m pytest -v
python workers/sync_nfl_postgame_to_neon_r2.py --validate-if-unconfigured
```

In public CI or local runs without sync secrets configured, `--validate-if-unconfigured` validates `data/2026/latest/master_dataset.json` (`3` tables, `50` rows) and exits `0` cleanly without attempting external network sync.

## Sources and Attribution

The tables in this repository contain strictly factual public-domain NFL standings and final game scores derived from `factual_nfl_postgame` (32 NFL standings rows, 13 Week 11 scores, and 5 Week 12 holiday slate scores). Third-party `pfr_*`, `sharp_*`, and `savant_*` CSV files and tables were removed under clean-room MIT compliance review (CF-17).

The MIT license applies to the code in this repository and to the GSE-authored structure (file layout, schema, and tooling) only. Third-party data remains subject to its owners' terms. Rights holders who want any table changed or removed can open an issue in this repository.

## License

MIT - see [LICENSE](LICENSE).
