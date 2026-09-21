-- news-radar schema. Apply with:
--     docker exec -i news-radar-db psql -U newsradar -d newsradar -v ON_ERROR_STOP=1 < schema.sql
-- Idempotent. Views are CREATE OR REPLACE and cannot change their column list;
-- adding a column to a view means DROP VIEW first, then re-apply this file.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;

-- A source is an adapter instance. `kind` names the adapter module in
-- newsradar/adapters/; `config` is whatever that adapter needs.
CREATE TABLE IF NOT EXISTS source (
    id         SERIAL PRIMARY KEY,
    kind       TEXT        NOT NULL,
    name       TEXT        NOT NULL UNIQUE,
    config     JSONB       NOT NULL DEFAULT '{}',
    enabled    BOOLEAN     NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO source (kind, name) VALUES ('gdelt', 'gdelt-events')
ON CONFLICT (name) DO NOTHING;

-- One row per fetched file, so a re-run skips what it has already loaded and
-- a gap in the series is visible as a missing row rather than as nothing.
CREATE TABLE IF NOT EXISTS fetch_log (
    source_id  INT         NOT NULL REFERENCES source(id),
    file       TEXT        NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_seen  INT         NOT NULL,
    rows_kept  INT         NOT NULL,
    PRIMARY KEY (source_id, file)
);

-- The normalized event. Column names follow GDELT's codebook because it is the
-- first adapter and its vocabulary (CAMEO, QuadClass, Goldstein) is the one
-- the agent is told about; other adapters map into it and leave what they
-- lack NULL.
CREATE TABLE IF NOT EXISTS event (
    id             BIGSERIAL PRIMARY KEY,
    source_id      INT         NOT NULL REFERENCES source(id),
    external_id    TEXT        NOT NULL,
    -- The date the event is reported to have happened (GDELT SQLDATE). Often
    -- far from added_at: anniversaries and retrospectives are events too.
    occurred_on    DATE,
    -- When the source first saw it. Every attention series is keyed on this.
    added_at       TIMESTAMPTZ NOT NULL,
    cameo_code     TEXT,
    cameo_root     TEXT,
    quad_class     SMALLINT,
    goldstein      REAL,
    tone           REAL,
    actor1_name    TEXT,
    actor1_country TEXT,
    actor2_name    TEXT,
    actor2_country TEXT,
    geo_type       SMALLINT,
    geo_name       TEXT,
    country        TEXT        NOT NULL,   -- FIPS 10-4 two-letter, as GDELT codes it
    adm1           TEXT,                   -- GDELT ADM1 code, country-prefixed (USWA)
    geom           geometry(Point, 4326) NOT NULL,
    -- Counted by GDELT within the 15-minute window the event first appeared
    -- in. A burst measure, which is what the acute rule wants.
    num_mentions   INT         NOT NULL,
    num_sources    INT         NOT NULL,
    num_articles   INT         NOT NULL,
    url            TEXT,
    UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS event_added_brin ON event USING brin (added_at);
CREATE INDEX IF NOT EXISTS event_geom_gist  ON event USING gist (geom);
CREATE INDEX IF NOT EXISTS event_country_added ON event (country, added_at);
CREATE INDEX IF NOT EXISTS event_sources_added ON event (added_at) WHERE num_sources >= 10;

-- Attention: how much the world's news is pointed at a place. Rolled up from
-- event by rollup.py; hourly kept 90 days, daily forever.
CREATE TABLE IF NOT EXISTS attention_hourly (
    region_kind TEXT        NOT NULL,   -- 'country' | 'adm1'
    region      TEXT        NOT NULL,
    hour        TIMESTAMPTZ NOT NULL,
    events      INT         NOT NULL,
    mentions    INT         NOT NULL,
    sources     INT         NOT NULL,
    PRIMARY KEY (region_kind, region, hour)
);
CREATE INDEX IF NOT EXISTS attention_hourly_hour ON attention_hourly (hour);

CREATE TABLE IF NOT EXISTS attention_daily (
    region_kind TEXT NOT NULL,
    region      TEXT NOT NULL,
    day         DATE NOT NULL,
    events      INT  NOT NULL,
    mentions    INT  NOT NULL,
    sources     INT  NOT NULL,
    PRIMARY KEY (region_kind, region, day)
);

-- Anomaly: each recent hour against the same hour of day over the trailing 30
-- days, absent rows counted as zero. Materialized by rollup.py for the last 48
-- hours rather than a view, because the 30-way self-join per row is too slow
-- to run on every map load. sd_floor is the Poisson floor: a region that is
-- normally silent and then gets 200 mentions must score, not divide by zero.
CREATE TABLE IF NOT EXISTS attention_anomaly (
    region_kind   TEXT        NOT NULL,
    region        TEXT        NOT NULL,
    hour          TIMESTAMPTZ NOT NULL,
    mentions      INT         NOT NULL,
    baseline_mean REAL        NOT NULL,
    baseline_sd   REAL        NOT NULL,
    z             REAL        NOT NULL,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (region_kind, region, hour)
);
CREATE INDEX IF NOT EXISTS attention_anomaly_z ON attention_anomaly (hour, z DESC);

-- What the detector has sent. Its own record; nothing else journals it.
CREATE TABLE IF NOT EXISTS alert (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT        NOT NULL,   -- 'region' | 'event'
    key        TEXT        NOT NULL,   -- region code or event id
    payload    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at    TIMESTAMPTZ,
    sink       TEXT
);
CREATE INDEX IF NOT EXISTS alert_kind_key_created ON alert (kind, key, created_at DESC);

-- Reserved for the region/topic watch layer (R10). Empty until it is built.
CREATE TABLE IF NOT EXISTS watch (
    id          SERIAL PRIMARY KEY,
    pattern     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    min_sources INT  NOT NULL DEFAULT 2,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-source health: is each adapter still delivering.
CREATE OR REPLACE VIEW source_health AS
SELECT s.name,
       s.kind,
       s.enabled,
       max(f.fetched_at)                                       AS last_fetch,
       count(*) FILTER (WHERE f.fetched_at > now() - interval '1 day') AS files_24h,
       sum(f.rows_kept) FILTER (WHERE f.fetched_at > now() - interval '1 day') AS rows_24h
FROM source s
LEFT JOIN fetch_log f ON f.source_id = s.id
GROUP BY s.id;

-- Region display names as the source itself labels them, refreshed by
-- rollup.py from country-level events. Covers the microstates that a 110m
-- country file lacks, and later ADM1 codes, without a hand-kept table.
CREATE TABLE IF NOT EXISTS region_name (
    region_kind TEXT NOT NULL,
    region      TEXT NOT NULL,
    name        TEXT NOT NULL,
    PRIMARY KEY (region_kind, region)
);
