-- PostgreSQL schema for OverFast API persistent storage

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'static_data_category') THEN
        CREATE TYPE static_data_category AS ENUM ('heroes', 'hero', 'gamemodes', 'maps', 'roles');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS static_data (
    key          VARCHAR(255)           PRIMARY KEY,
    data         BYTEA                  NOT NULL,
    category     static_data_category   NOT NULL,
    data_version SMALLINT               NOT NULL DEFAULT 1,
    created_at   TIMESTAMPTZ            NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ            NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS player_profiles (
    player_id               TEXT        PRIMARY KEY,
    battletag               TEXT,
    name                    TEXT,
    html_compressed         BYTEA       NOT NULL,
    summary                 JSONB,
    last_updated_blizzard   BIGINT,
    data_version            SMALLINT    NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_player_profiles_updated_at
    ON player_profiles (updated_at);

CREATE INDEX IF NOT EXISTS idx_player_profiles_battletag
    ON player_profiles (battletag)
    WHERE battletag IS NOT NULL;

-- Key columns are TEXT rather than VARCHAR(n): storage is identical in
-- PostgreSQL, but a length limit would abort an entire snapshot flush with
-- "value too long" the first time Blizzard ships a hero or map key longer than
-- the limit — exactly the kind of new-release hard failure this API avoids.
CREATE TABLE IF NOT EXISTS hero_stats_snapshots (
    id          BIGSERIAL        PRIMARY KEY,
    captured_at TIMESTAMPTZ      NOT NULL,
    platform    TEXT             NOT NULL,
    gamemode    TEXT             NOT NULL,
    region      TEXT             NOT NULL,
    map         TEXT             NOT NULL,
    tier        TEXT             NOT NULL,
    hero        TEXT             NOT NULL,
    pickrate    DOUBLE PRECISION NOT NULL,
    winrate     DOUBLE PRECISION NOT NULL,
    banrate     DOUBLE PRECISION
);

-- Migrations for existing databases (idempotent).
ALTER TABLE hero_stats_snapshots ADD COLUMN IF NOT EXISTS banrate DOUBLE PRECISION;

-- Widen the pre-existing VARCHAR(n) key columns to TEXT. Guarded by a lookup so
-- re-running this file (it executes on every initialize()) is a no-op once the
-- columns are already TEXT. VARCHAR -> TEXT is binary coercible, so PostgreSQL
-- rewrites no data, but it still takes a brief ACCESS EXCLUSIVE lock.
DO $$
DECLARE
    column_to_widen TEXT;
BEGIN
    FOR column_to_widen IN
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'hero_stats_snapshots'
          AND column_name IN ('platform', 'gamemode', 'region', 'map', 'tier', 'hero')
          AND data_type = 'character varying'
    LOOP
        EXECUTE format(
            'ALTER TABLE hero_stats_snapshots ALTER COLUMN %I TYPE TEXT',
            column_to_widen
        );
    END LOOP;
END $$;

-- Indexes for a table that grows without bound (retention is disabled by
-- default). NOTE: these CREATE INDEX statements take an ACCESS EXCLUSIVE lock
-- and block writes for the duration. That is fine on an empty/new database, but
-- any index added here *after* launch must first be created out-of-band with
-- CREATE INDEX CONCURRENTLY on the live database — otherwise the next
-- initialize() stalls the app while it builds the index over the whole table.
--
-- Serves fully-qualified lookups (region/map/tier/hero all given).
CREATE INDEX IF NOT EXISTS idx_hero_stats_snapshots_query
    ON hero_stats_snapshots (platform, gamemode, region, map, tier, hero, captured_at);

-- Serves the common case where region/map/tier are omitted: history queries
-- filtered only by the required columns, and the DISTINCT captured_at
-- ORDER BY captured_at DESC date listing, which cannot use the index above
-- because captured_at sits behind four optional columns.
CREATE INDEX IF NOT EXISTS idx_hero_stats_snapshots_captured_at
    ON hero_stats_snapshots (platform, gamemode, captured_at DESC);
