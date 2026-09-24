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

-- attention: whether this source's events count toward the attention series.
-- News sources do; primary feeds (a quake, a flood alert) are ground truth,
-- not coverage, and are shown as their own layer instead.
ALTER TABLE source ADD COLUMN IF NOT EXISTS attention BOOLEAN NOT NULL DEFAULT true;

INSERT INTO source (kind, name, attention) VALUES
    ('gdelt', 'gdelt-events', true),
    ('usgs',  'usgs-quakes',  false),
    ('gdacs', 'gdacs-alerts', false)
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
    -- Adapter-specific fields a generic column cannot hold: USGS magnitude
    -- and depth, GDACS alert level and event type. Kept as JSON on purpose;
    -- the agent can read it and no schema change is needed per adapter.
    props          JSONB,
    UNIQUE (source_id, external_id)
);
ALTER TABLE event ADD COLUMN IF NOT EXISTS props JSONB;

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

-- Per-source health (R32): is each adapter still delivering. expect_every is
-- how often the source should land rows; stale means no fetch that kept rows
-- in three of those intervals, which catches both a stopped timer and a feed
-- that runs and returns nothing.
ALTER TABLE source ADD COLUMN IF NOT EXISTS expect_every INTERVAL NOT NULL DEFAULT '15 minutes';

DROP VIEW IF EXISTS source_health;
CREATE VIEW source_health AS
SELECT s.name,
       s.kind,
       s.enabled,
       s.expect_every,
       max(f.fetched_at)                                                  AS last_fetch,
       max(f.fetched_at) FILTER (WHERE f.rows_kept > 0)                   AS last_rows,
       count(f.*) FILTER (WHERE f.fetched_at > now() - interval '1 day')  AS fetches_24h,
       coalesce(sum(f.rows_kept) FILTER (WHERE f.fetched_at > now() - interval '1 day'), 0) AS rows_24h,
       s.enabled AND coalesce(max(f.fetched_at) FILTER (WHERE f.rows_kept > 0), '-infinity')
                     < now() - 3 * s.expect_every                         AS stale
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

-- Headlines for story URLs (R16). GDELT carries no title; this is fetched
-- from the page. One row per URL regardless of how many events cite it.
CREATE TABLE IF NOT EXISTS story (
    url        TEXT        PRIMARY KEY,
    title      TEXT,
    site       TEXT,
    status     TEXT        NOT NULL,   -- 'ok' | 'fail'
    attempts   INT         NOT NULL DEFAULT 1,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS story_retry ON story (fetched_at) WHERE status = 'fail';

-- Country polygons (Natural Earth 110m, FIPS-keyed) for reverse geocoding
-- sources that give a point but no country. Loaded by scripts/load_countries.py
-- from web/public/countries.geojson so the map and the database agree.
CREATE TABLE IF NOT EXISTS country_shape (
    fips TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    geom geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS country_shape_gist ON country_shape USING gist (geom);

-- GDELT Mentions (R18). Its own source row so source_health sees it; it
-- produces no event rows, only mentions of gdelt-events rows.
INSERT INTO source (kind, name, attention) VALUES ('gdelt', 'gdelt-mentions', true)
ON CONFLICT (name) DO NOTHING;

-- Raw mentions, a 72 h buffer. Distinct outlets per hour, per region and per
-- growth window cannot be summed across 15-minute files, so they are counted
-- from here and everything long-lived is rolled up (rollup.py).
CREATE TABLE IF NOT EXISTS mention (
    event_id     BIGINT      NOT NULL,
    mentioned_at TIMESTAMPTZ NOT NULL,
    source_name  TEXT        NOT NULL
);
CREATE INDEX IF NOT EXISTS mention_at_brin ON mention USING brin (mentioned_at);
CREATE INDEX IF NOT EXISTS mention_event ON mention (event_id, mentioned_at);

-- Coverage per event per hour, kept 90 days like event.
CREATE TABLE IF NOT EXISTS mention_hourly (
    event_id BIGINT      NOT NULL,
    hour     TIMESTAMPTZ NOT NULL,
    mentions INT         NOT NULL,
    sources  INT         NOT NULL,
    PRIMARY KEY (event_id, hour)
);
CREATE INDEX IF NOT EXISTS mention_hourly_hour ON mention_hourly (hour);

-- Headline normalization for syndication collapse (R21): lowercase, drop a
-- trailing " - Site" or " | Site", strip punctuation, collapse whitespace.
-- Reprints of one wire story then share a key across sites.
CREATE OR REPLACE FUNCTION norm_title(t TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT nullif(btrim(regexp_replace(regexp_replace(
               regexp_replace(lower(t), '\s+[-|\u2013\u2014:]\s+[^-|\u2013\u2014:]{2,40}$', ''),
               '[^[:alnum:][:space:]]', '', 'g'),
           '\s+', ' ', 'g')), '')
$$;

-- Growth per event (R20): outlets and mentions in the last 1 h and 6 h,
-- against the first-window counts on the event row. Reads the raw buffer, so
-- sources are exact distinct counts.
CREATE OR REPLACE VIEW event_growth AS
SELECT e.id AS event_id, e.url, e.added_at,
       e.num_sources AS first_sources, e.num_mentions AS first_mentions,
       count(*) FILTER (WHERE m.mentioned_at > now() - interval '1 hour')                       AS mentions_1h,
       count(DISTINCT m.source_name) FILTER (WHERE m.mentioned_at > now() - interval '1 hour')  AS sources_1h,
       count(*)                                                                                  AS mentions_6h,
       count(DISTINCT m.source_name)                                                             AS sources_6h
FROM mention m JOIN event e ON e.id = m.event_id
WHERE m.mentioned_at > now() - interval '6 hours'
GROUP BY e.id;
