-- Galaxy Sports Edge (GSE) - Bitemporal Post-Game NFL Analytics Schema
-- Target: Neon Serverless Postgres (summer-brook-99380762) over HTTPS :443
-- Zero Render build minutes | Zero GitHub Actions minutes | Compatible with Vercel ISR

CREATE SCHEMA IF NOT EXISTS gse_nfl_stats;

CREATE TABLE IF NOT EXISTS gse_nfl_stats.dataset_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    season              INTEGER NOT NULL,
    nfl_week            INTEGER NOT NULL,
    window_tag          TEXT NOT NULL, -- e.g. 'WK11_TNF', 'WK11_SUN_EARLY', 'WK11_SNF', 'WK11_MNF'
    ingested_at_utc     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_manifest     JSONB NOT NULL,
    table_count         INTEGER NOT NULL,
    total_rows          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS gse_nfl_stats.stat_tables_bitemporal (
    table_key           TEXT NOT NULL,
    season              INTEGER NOT NULL,
    nfl_week            INTEGER NOT NULL,
    window_tag          TEXT NOT NULL,
    source_url          TEXT NOT NULL,
    source_as_of        TEXT NOT NULL,
    ingested_at_utc     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    columns_json        JSONB NOT NULL,
    rows_json           JSONB NOT NULL,
    row_count           INTEGER NOT NULL,
    PRIMARY KEY (table_key, season, nfl_week, window_tag)
);

CREATE OR REPLACE VIEW gse_nfl_stats.v_latest_stat_tables AS
SELECT DISTINCT ON (table_key)
    table_key,
    season,
    nfl_week,
    window_tag,
    source_url,
    source_as_of,
    ingested_at_utc,
    columns_json,
    rows_json,
    row_count
FROM gse_nfl_stats.stat_tables_bitemporal
ORDER BY table_key, ingested_at_utc DESC;
