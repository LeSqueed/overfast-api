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

CREATE TABLE IF NOT EXISTS hero_stats_snapshots (
    id          BIGSERIAL        PRIMARY KEY,
    captured_at TIMESTAMPTZ      NOT NULL,
    platform    VARCHAR(10)      NOT NULL,
    gamemode    VARCHAR(20)      NOT NULL,
    region      VARCHAR(20)      NOT NULL,
    map         VARCHAR(50)      NOT NULL,
    tier        VARCHAR(20)      NOT NULL,
    hero        VARCHAR(50)      NOT NULL,
    pickrate    DOUBLE PRECISION NOT NULL,
    winrate     DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hero_stats_snapshots_query
    ON hero_stats_snapshots (platform, gamemode, region, map, tier, hero, captured_at);
