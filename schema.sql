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

-- Stories ranked by coverage (R20, R21). A story is the event URLs sharing a
-- normalized headline (URL alone when there is no headline yet); its rank is
-- distinct outlets mentioning any of its events within the window, from the
-- mention buffer, so the window caps at 72 h. rep is the story's most
-- mentioned event, whose place and URL stand for it on the map.
CREATE OR REPLACE FUNCTION top_stories(p_hours INT, p_limit INT)
RETURNS TABLE (skey TEXT, rep BIGINT, outlets INT, outlets_1h INT, mentions INT, sites INT)
LANGUAGE sql STABLE AS $$
    WITH per_event AS (
        SELECT event_id, count(*) AS mentions
        FROM mention
        WHERE mentioned_at > now() - make_interval(hours => p_hours)
        GROUP BY event_id
    ), keyed AS (
        SELECT p.event_id, p.mentions, s.site, coalesce(norm_title(s.title), e.url, e.id::text) AS skey
        FROM per_event p JOIN event e ON e.id = p.event_id
        LEFT JOIN story s ON s.url = e.url AND s.status = 'ok'
    )
    SELECT k.skey,
           (array_agg(k.event_id ORDER BY k.mentions DESC, k.event_id))[1],
           count(DISTINCT m.source_name)::int,
           (count(DISTINCT m.source_name) FILTER (WHERE m.mentioned_at > now() - interval '1 hour'))::int,
           count(*)::int,
           count(DISTINCT k.site)::int
    FROM keyed k
    JOIN mention m ON m.event_id = k.event_id AND m.mentioned_at > now() - make_interval(hours => p_hours)
    GROUP BY k.skey
    ORDER BY 3 DESC, 4 DESC, 1
    LIMIT p_limit
$$;

-- GKG, slim (R22). Raw GKG is ~1 GB/day, so only these columns are kept, 30
-- days. Every place an article names, not one action geo per coded event.
INSERT INTO source (kind, name, attention) VALUES ('gdelt', 'gdelt-gkg', true)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS gkg_article (
    id       BIGSERIAL   PRIMARY KEY,
    gkg_id   TEXT        NOT NULL UNIQUE,
    added_at TIMESTAMPTZ NOT NULL,
    url      TEXT,
    site     TEXT,
    tone     REAL,
    themes   TEXT[]      NOT NULL
);
CREATE INDEX IF NOT EXISTS gkg_article_added_brin ON gkg_article USING brin (added_at);

CREATE TABLE IF NOT EXISTS gkg_location (
    article_id BIGINT   NOT NULL REFERENCES gkg_article(id) ON DELETE CASCADE,
    loc_type   SMALLINT NOT NULL,
    country    TEXT     NOT NULL,
    adm1       TEXT,                 -- never set for type 1 (country centroid)
    geom       geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS gkg_location_article ON gkg_location (article_id);
CREATE INDEX IF NOT EXISTS gkg_location_country ON gkg_location (country);

-- Articles per theme per region. Written additively at ingest, one file at a
-- time inside that file's transaction: each article is in exactly one file and
-- fetch_log loads a file once, so += is exact and nothing is re-expanded.
CREATE TABLE IF NOT EXISTS theme_daily (
    region_kind TEXT NOT NULL,   -- 'country' | 'adm1'
    region      TEXT NOT NULL,
    theme       TEXT NOT NULL,
    day         DATE NOT NULL,
    articles    INT  NOT NULL,
    PRIMARY KEY (region_kind, region, theme, day)
);

CREATE TABLE IF NOT EXISTS theme_hourly (
    region   TEXT        NOT NULL,   -- country only
    theme    TEXT        NOT NULL,
    hour     TIMESTAMPTZ NOT NULL,
    articles INT         NOT NULL,
    PRIMARY KEY (region, theme, hour)
);
CREATE INDEX IF NOT EXISTS theme_hourly_hour ON theme_hourly (hour);

-- NWS alerts (R24). A later message's references mark what it supersedes.
ALTER TABLE event ADD COLUMN IF NOT EXISTS superseded_by TEXT;
INSERT INTO source (kind, name, attention) VALUES ('nws', 'nws-alerts', false)
ON CONFLICT (name) DO NOTHING;

-- NWS zone polygons, fetched from api.weather.gov on first reference and
-- refetched after 90 days; geom NULL for a zone the API has no shape for.
CREATE TABLE IF NOT EXISTS nws_zone (
    url        TEXT PRIMARY KEY,
    code       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    name       TEXT,
    geom       geometry(MultiPolygon, 4326),
    fetched_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS nws_zone_gist ON nws_zone USING gist (geom);

-- What the map's ground-truth layer draws now: one rule per source kind, so
-- each adapter adds its own clause here. The store keeps everything; this is
-- only what is current and above each source's noise floor.
DROP VIEW IF EXISTS primary_live;
CREATE VIEW primary_live AS
SELECT e.id, s.kind AS source, e.external_id, e.added_at, e.occurred_on, e.geo_name, e.country,
       ST_Y(e.geom)::float AS lat, ST_X(e.geom)::float AS lon, e.url, e.props
FROM event e JOIN source s ON s.id = e.source_id
WHERE NOT s.attention AND CASE s.kind
    WHEN 'usgs'  THEN e.added_at > now() - interval '72 hours'
    -- GDACS iscurrent is false on live Orange droughts; modified is the filter.
    WHEN 'gdacs' THEN coalesce(e.props->>'alert', '') <> 'Green'
                      AND (e.props->>'modified')::timestamptz > now() - interval '30 days'
    -- Severe and Extreme only: Small Craft and Gale advisories are most of the feed.
    WHEN 'nws'   THEN e.superseded_by IS NULL
                      AND (e.props->>'expires')::timestamptz > now()
                      AND e.props->>'severity' IN ('Severe', 'Extreme')
    -- Information means an earthquake happened and there is no threat.
    WHEN 'tsunami' THEN e.props->>'category' IS DISTINCT FROM 'Information'
                      AND e.added_at > now() - interval '24 hours'
    -- Only elevated volcanoes are listed; one not seen lately has gone back to Green.
    WHEN 'volcano' THEN (e.props->>'seen')::timestamptz > now() - interval '3 hours'
    -- 50+ detections and burning in the last day: ~260 large fires on
    -- 2026-09-24, not the ~47k small burns a day produces (20+ gave 1,071).
    WHEN 'firms' THEN (e.props->>'detections')::int >= 50
                      AND (e.props->>'last_seen')::timestamptz > now() - interval '24 hours'
    -- Started in the last 48 h and still going or ended in the last 3 h.
    -- Multi-day region "outages" are chronic measurement conditions.
    WHEN 'ioda' THEN (e.props->>'end')::timestamptz > now() - interval '3 hours'
                      AND e.added_at > now() - interval '48 hours'
    -- Ongoing, or ended in the last 3 h.
    WHEN 'radar' THEN coalesce((e.props->>'end')::timestamptz, 'infinity') > now() - interval '3 hours'
    ELSE false END;

-- Tsunami bulletins (R25). Polled every 5 min by news-radar-fast.timer.
INSERT INTO source (kind, name, attention, expect_every) VALUES ('tsunami', 'tsunami-bulletins', false, '5 minutes')
ON CONFLICT (name) DO NOTHING;

-- USGS elevated volcanoes (R26).
INSERT INTO source (kind, name, attention) VALUES ('volcano', 'usgs-volcanoes', false)
ON CONFLICT (name) DO NOTHING;

-- NASA FIRMS (R27): raw VIIRS detections, 30 days, and the fires they form.
INSERT INTO source (kind, name, attention, expect_every) VALUES ('firms', 'firms-fires', false, '3 hours')
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS firms_detection (
    id         BIGSERIAL   PRIMARY KEY,
    satellite  TEXT        NOT NULL,     -- N (Suomi NPP), N20, N21
    lat        DOUBLE PRECISION NOT NULL,
    lon        DOUBLE PRECISION NOT NULL,
    acq_at     TIMESTAMPTZ NOT NULL,
    frp        REAL,                     -- fire radiative power, MW
    confidence TEXT,                     -- low | nominal | high
    daynight   TEXT,
    bright_ti4 REAL,
    geom       geometry(Point, 4326) NOT NULL,
    fire_id    BIGINT,                   -- event.id of the fire it belongs to
    UNIQUE (satellite, lat, lon, acq_at)
);
CREATE INDEX IF NOT EXISTS firms_detection_geom ON firms_detection USING gist (geom);
CREATE INDEX IF NOT EXISTS firms_detection_unassigned ON firms_detection (id) WHERE fire_id IS NULL;
CREATE INDEX IF NOT EXISTS firms_detection_fire ON firms_detection (fire_id);
CREATE INDEX IF NOT EXISTS firms_detection_acq_brin ON firms_detection USING brin (acq_at);

CREATE TABLE IF NOT EXISTS fire (
    event_id   BIGINT PRIMARY KEY,
    hull       geometry(Geometry, 4326) NOT NULL,
    detections INT  NOT NULL,
    high_conf  INT  NOT NULL,
    frp_sum    REAL NOT NULL,
    frp_max    REAL NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS fire_hull ON fire USING gist (hull);
CREATE INDEX IF NOT EXISTS fire_last_seen ON fire (last_seen);

-- ISO to FIPS (GDELT codes countries in FIPS 10-4; most other sources use
-- ISO). From GeoNames countryInfo.txt, CC BY 4.0, via
-- scripts/load_country_codes.py.
CREATE TABLE IF NOT EXISTS country_code (
    iso2 TEXT PRIMARY KEY,
    iso3 TEXT NOT NULL,
    fips TEXT,
    name TEXT NOT NULL
);

-- IODA internet outages (R28). All rights reserved by Georgia Tech Research
-- Corporation: ingested for personal use, never republished.
INSERT INTO source (kind, name, attention) VALUES ('ioda', 'ioda-outages', false)
ON CONFLICT (name) DO NOTHING;

-- IODA region entity -> country, looked up once per region code.
CREATE TABLE IF NOT EXISTS ioda_region (
    code         TEXT PRIMARY KEY,
    name         TEXT,
    country_iso  TEXT,
    ne_region_id TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cloudflare Radar outage annotations (R29). Seeded disabled so an instance
-- without a token does not show it stale; enable after setting the token.
INSERT INTO source (kind, name, attention, expect_every, enabled) VALUES ('radar', 'cloudflare-radar', false, '1 day', false)
ON CONFLICT (name) DO NOTHING;
