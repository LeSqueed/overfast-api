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

-- One row per snapshot run. Claiming a row reserves the run slot, so a second
-- scheduler replica, a task redelivery or a manual re-run stands down instead
-- of walking the grid a second time; completing it records that the grid was
-- walked to the end. A slot with an unfinished run is hidden from
-- get_hero_stats_history_dates, so clients never chart a half-written day.
-- One row per run (daily by default) keeps this table tiny even though
-- hero_stats_snapshots grows without bound.
CREATE TABLE IF NOT EXISTS hero_stats_snapshot_runs (
    captured_at   TIMESTAMPTZ PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    row_count     BIGINT      NOT NULL DEFAULT 0,
    skipped_count INTEGER     NOT NULL DEFAULT 0
);

-- Indexes for a table that grows without bound (retention is disabled by
-- default). NOTE: these CREATE INDEX statements take an ACCESS EXCLUSIVE lock
-- and block writes for the duration. That is fine on an empty/new database, but
-- any index added here *after* launch must first be created out-of-band with
-- CREATE INDEX CONCURRENTLY on the live database — otherwise the next
-- initialize() stalls the app while it builds the index over the whole table.
--
-- Serves the common case where region/map/tier are omitted: history queries
-- filtered only by the required columns, and the DISTINCT captured_at
-- ORDER BY captured_at DESC date listing, which cannot use the unique index
-- below because captured_at sits behind four optional columns.
CREATE INDEX IF NOT EXISTS idx_hero_stats_snapshots_captured_at
    ON hero_stats_snapshots (platform, gamemode, captured_at DESC);

-- Serves fully-qualified lookups (region/map/tier/hero all given) *and* doubles
-- as the uniqueness key: one row per grid cell per snapshot slot. It holds
-- exactly the seven columns of the former idx_hero_stats_snapshots_query, so
-- enforcing uniqueness costs no extra storage or write amplification on a table
-- that is never pruned — while letting store_hero_stats_snapshots() use
-- ON CONFLICT DO UPDATE, which makes a repeated or resumed run overwrite its
-- own rows instead of duplicating the grid. It replaces that index rather than
-- sitting next to it; idx_hero_stats_snapshots_captured_at above is untouched,
-- as it serves an access path this index cannot.
--
-- On an existing database this is a one-off blocking index build (plus a
-- one-off deduplication of any grid already written twice); the guard makes it
-- run only once, on the first startup after the upgrade.
DO $$
BEGIN
    -- Serialize this migration across the app/worker/scheduler replicas, which
    -- all run schema.sql on startup. Without a lock, two replicas can both pass
    -- the index-existence guard below and then collide on the DDL.
    PERFORM pg_advisory_xact_lock(hashtext('overfast:hero_stats_snapshots_unique'));

    IF EXISTS (
        SELECT 1 FROM pg_class
        WHERE relname = 'idx_hero_stats_snapshots_unique' AND relkind = 'i'
    ) THEN
        RETURN;
    END IF;

    -- Keep the first row written for each grid cell; drop later duplicates.
    -- Window-function dedup instead of a self-join: a self-join over the
    -- growing table is O(n^2) and stalls the migration on a large database,
    -- even when there are no duplicates to remove.
    DELETE FROM hero_stats_snapshots
    WHERE id IN (
        SELECT id
        FROM (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY captured_at, platform, gamemode, region,
                                    map, tier, hero
                       ORDER BY id
                   ) AS rn
            FROM hero_stats_snapshots
        ) ranked
        WHERE rn > 1
    );

    CREATE UNIQUE INDEX idx_hero_stats_snapshots_unique
        ON hero_stats_snapshots (platform, gamemode, region, map, tier, hero, captured_at);

    DROP INDEX IF EXISTS idx_hero_stats_snapshots_query;
END $$;
