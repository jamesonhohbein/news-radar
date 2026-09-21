"""Writes: events in, fetches logged. Idempotent on (source, external_id) and
on (source, file), so any run can be repeated."""
from __future__ import annotations

from dataclasses import astuple
from typing import Iterable

import psycopg

from .adapters import Event

_COLS = ("external_id", "occurred_on", "added_at", "cameo_code", "cameo_root", "quad_class",
         "goldstein", "tone", "actor1_name", "actor1_country", "actor2_name", "actor2_country",
         "geo_type", "geo_name", "country", "adm1", "lat", "lon",
         "num_mentions", "num_sources", "num_articles", "url")


def source_id(conn: psycopg.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM source WHERE name = %s AND enabled", (name,)).fetchone()
    if not row:
        raise SystemExit(f"source {name!r} missing or disabled")
    return row[0]


def already_fetched(conn: psycopg.Connection, sid: int, files: Iterable[str]) -> set[str]:
    rows = conn.execute("SELECT file FROM fetch_log WHERE source_id = %s AND file = ANY(%s)",
                        (sid, list(files))).fetchall()
    return {r[0] for r in rows}


def insert_events(conn: psycopg.Connection, sid: int, events: Iterable[Event]) -> int:
    """COPY into a temp table, then INSERT ... ON CONFLICT DO NOTHING. COPY is
    an order of magnitude faster than executemany for a backfill, and the
    temp-table hop is what makes the conflict clause possible."""
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE staging (LIKE event INCLUDING DEFAULTS) ON COMMIT DROP")
        cur.execute("ALTER TABLE staging DROP COLUMN id, DROP COLUMN geom, DROP COLUMN source_id, "
                    "ADD COLUMN lat double precision, ADD COLUMN lon double precision")
        n = 0
        with cur.copy(f"COPY staging ({', '.join(_COLS)}) FROM STDIN") as copy:
            for e in events:
                copy.write_row(astuple(e))
                n += 1
        if n == 0:
            return 0
        cur.execute(f"""
            INSERT INTO event (source_id, geom, {', '.join(c for c in _COLS if c not in ('lat', 'lon'))})
            SELECT %s, ST_SetSRID(ST_MakePoint(lon, lat), 4326),
                   {', '.join(c for c in _COLS if c not in ('lat', 'lon'))}
            FROM staging
            ON CONFLICT (source_id, external_id) DO NOTHING
        """, (sid,))
        return cur.rowcount


def log_fetch(conn: psycopg.Connection, sid: int, file: str, seen: int, kept: int) -> None:
    conn.execute("""INSERT INTO fetch_log (source_id, file, rows_seen, rows_kept)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (source_id, file) DO UPDATE
                    SET fetched_at = now(), rows_seen = EXCLUDED.rows_seen, rows_kept = EXCLUDED.rows_kept""",
                 (sid, file, seen, kept))
