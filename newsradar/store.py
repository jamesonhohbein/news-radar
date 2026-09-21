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
