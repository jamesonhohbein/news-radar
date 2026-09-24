"""Writes: events in, fetches logged. Idempotent on (source, external_id) and
on (source, file), so any run can be repeated."""
from __future__ import annotations

from dataclasses import astuple
from typing import Iterable

import psycopg
from psycopg.types.json import Jsonb

from .adapters import Event

_COLS = ("external_id", "occurred_on", "added_at", "cameo_code", "cameo_root", "quad_class",
         "goldstein", "tone", "actor1_name", "actor1_country", "actor2_name", "actor2_country",
         "geo_type", "geo_name", "country", "adm1", "lat", "lon",
         "num_mentions", "num_sources", "num_articles", "url", "props")


def source_id(conn: psycopg.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM source WHERE name = %s AND enabled", (name,)).fetchone()
    if not row:
        raise SystemExit(f"source {name!r} missing or disabled")
    return row[0]


def already_fetched(conn: psycopg.Connection, sid: int, files: Iterable[str]) -> set[str]:
    rows = conn.execute("SELECT file FROM fetch_log WHERE source_id = %s AND file = ANY(%s)",
                        (sid, list(files))).fetchall()
    return {r[0] for r in rows}


def insert_events(conn: psycopg.Connection, sid: int, events: Iterable[Event], update: bool = False) -> int:
    """COPY into a temp table, then INSERT ... ON CONFLICT. COPY is an order
    of magnitude faster than executemany for a backfill, and the temp-table
    hop is what makes the conflict clause possible. update=True is for feed
    adapters whose events are revised in place (a quake's magnitude, an
    alert's level): the row is refreshed rather than skipped."""
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE staging (LIKE event INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute("ALTER TABLE staging DROP COLUMN id, DROP COLUMN geom, DROP COLUMN source_id, "
                    "ADD COLUMN lat double precision, ADD COLUMN lon double precision")
        n = 0
        with cur.copy(f"COPY staging ({', '.join(_COLS)}) FROM STDIN") as copy:
            for e in events:
                row = astuple(e)
                copy.write_row(row[:-1] + (Jsonb(row[-1]) if row[-1] is not None else None,))
                n += 1
        if n == 0:
            return 0
        cols = [c for c in _COLS if c not in ("lat", "lon")]
        # On update, keep a geocoded country when the adapter sends none.
        conflict = ("DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("external_id", "country"))
                    + ", geom = EXCLUDED.geom"
                    + ", country = CASE WHEN EXCLUDED.country = '' THEN event.country ELSE EXCLUDED.country END") if update else "DO NOTHING"
        cur.execute(f"""
            INSERT INTO event (source_id, geom, {', '.join(cols)})
            SELECT %s, ST_SetSRID(ST_MakePoint(lon, lat), 4326), {', '.join(cols)}
            FROM staging
            ON CONFLICT (source_id, external_id) {conflict}
        """, (sid,))
        return cur.rowcount


def reverse_geocode(conn: psycopg.Connection, sid: int, max_km: float = 100) -> int:
    """Fill country for a source's rows that arrived without one: the country
    polygon containing the point, else the nearest within max_km (110m
    coastlines are coarse and quakes are often offshore), else left empty,
    which the rollups already skip."""
    return conn.execute("""
        UPDATE event e
           SET country = coalesce(
                 (SELECT c.fips FROM country_shape c WHERE ST_Contains(c.geom, e.geom) LIMIT 1),
                 (SELECT c.fips FROM country_shape c
                   WHERE ST_DWithin(c.geom::geography, e.geom::geography, %s)
                   ORDER BY c.geom <-> e.geom LIMIT 1),
                 '')
         WHERE e.source_id = %s AND e.country = ''
    """, (max_km * 1000, sid)).rowcount


def log_fetch(conn: psycopg.Connection, sid: int, file: str, seen: int, kept: int) -> None:
    conn.execute("""INSERT INTO fetch_log (source_id, file, rows_seen, rows_kept)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (source_id, file) DO UPDATE
                    SET fetched_at = now(), rows_seen = EXCLUDED.rows_seen, rows_kept = EXCLUDED.rows_kept""",
                 (sid, file, seen, kept))


def insert_mentions(conn: psycopg.Connection, events_sid: int, rows: Iterable[tuple[str, object, str]]) -> tuple[int, int]:
    """Mentions of events we hold go into the raw buffer; the rest (events
    with no geo, or pruned) are counted and dropped. Returns (seen, kept).
    Idempotency is fetch_log's job: a file is loaded once."""
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE mstaging (gid text, mentioned_at timestamptz, source_name text) ON COMMIT DROP")
        n = 0
        with cur.copy("COPY mstaging FROM STDIN") as copy:
            for r in rows:
                copy.write_row(r)
                n += 1
        if n == 0:
            return 0, 0
        cur.execute("""
            INSERT INTO mention (event_id, mentioned_at, source_name)
            SELECT e.id, m.mentioned_at, m.source_name
            FROM mstaging m JOIN event e ON e.source_id = %s AND e.external_id = m.gid
        """, (events_sid,))
        return n, cur.rowcount


def insert_gkg(conn: psycopg.Connection, articles: Iterable) -> int:
    """GKG articles and their places, then their theme counts, in the caller's
    transaction. Returns articles inserted. A re-sent article (same gkg_id)
    inserts nothing and so adds nothing to the theme tables."""
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE gstaging (gkg_id text, added_at timestamptz, url text, site text, tone real, themes text[]) ON COMMIT DROP")
        cur.execute("CREATE TEMP TABLE lstaging (gkg_id text, loc_type smallint, country text, adm1 text, lat float8, lon float8) ON COMMIT DROP")
        # One COPY per connection at a time, so the file is held in memory
        # (under 1k articles per 15-minute file).
        articles = list(articles)
        n = len(articles)
        with cur.copy("COPY gstaging FROM STDIN") as copy:
            for a in articles:
                copy.write_row((a.gkg_id, a.added_at, a.url, a.site, a.tone, list(a.themes)))
        with cur.copy("COPY lstaging FROM STDIN") as copy:
            for a in articles:
                for l in a.locations:
                    copy.write_row((a.gkg_id, l.loc_type, l.country, l.adm1, l.lat, l.lon))
        if n == 0:
            return 0
        cur.execute("""
            CREATE TEMP TABLE gnew ON COMMIT DROP AS
            WITH ins AS (
                INSERT INTO gkg_article (gkg_id, added_at, url, site, tone, themes)
                SELECT gkg_id, added_at, url, site, tone, themes FROM gstaging
                ON CONFLICT (gkg_id) DO NOTHING
                RETURNING id, gkg_id, added_at, themes
            ) SELECT * FROM ins""")
        inserted = cur.rowcount
        cur.execute("""
            INSERT INTO gkg_location (article_id, loc_type, country, adm1, geom)
            SELECT g.id, l.loc_type, l.country, l.adm1, ST_SetSRID(ST_MakePoint(l.lon, l.lat), 4326)
            FROM lstaging l JOIN gnew g USING (gkg_id)""")
        # One count per article per (region, theme), however often it names the place.
        cur.execute("""
            WITH places AS (
                SELECT DISTINCT g.id, g.added_at, g.themes, 'country' AS kind, l.country AS region
                FROM gnew g JOIN lstaging l USING (gkg_id)
                UNION
                SELECT DISTINCT g.id, g.added_at, g.themes, 'adm1', l.adm1
                FROM gnew g JOIN lstaging l USING (gkg_id) WHERE l.adm1 IS NOT NULL
            ), x AS (
                SELECT kind, region, t AS theme, added_at FROM places, unnest(themes) AS t
            ), d AS (
                INSERT INTO theme_daily (region_kind, region, theme, day, articles)
                SELECT kind, region, theme, (added_at AT TIME ZONE 'UTC')::date, count(*) FROM x GROUP BY 1, 2, 3, 4
                ON CONFLICT (region_kind, region, theme, day) DO UPDATE SET articles = theme_daily.articles + EXCLUDED.articles
            )
            INSERT INTO theme_hourly (region, theme, hour, articles)
            SELECT region, theme, date_trunc('hour', added_at), count(*) FROM x WHERE kind = 'country' GROUP BY 1, 2, 3
            ON CONFLICT (region, theme, hour) DO UPDATE SET articles = theme_hourly.articles + EXCLUDED.articles""")
        return inserted
