#!/usr/bin/env python3
"""Consume Wikipedia's edit stream into wiki_edit (R30). Runs forever under
news-radar-wiki.service; resumes from stream_cursor.

    wiki_stream.py              # run
    wiki_stream.py --seconds 60 # run a minute, flush, exit (testing)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from newsradar import store
from newsradar.adapters import wikipedia as wp
from newsradar.db import connect

FLUSH_S = 10
GEO_EVERY_S = 60
GEO_BATCH = 200


def flush(conn, sid: int, rows: list[tuple], cursor: str | None) -> int:
    """Rows and the cursor that covers them, in one transaction."""
    kept = 0
    if rows:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE wstaging (wiki text, server text, title text, at timestamptz, "
                        "user_hash text, bot boolean, created boolean, rev bigint) ON COMMIT DROP")
            with cur.copy("COPY wstaging FROM STDIN") as copy:
                for r in rows:
                    copy.write_row(r)
            cur.execute("""INSERT INTO wiki_edit (wiki, server, title, at, user_hash, bot, created, rev)
                           SELECT * FROM wstaging ON CONFLICT (wiki, rev) DO NOTHING""")
            kept = cur.rowcount
        hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        conn.execute("""INSERT INTO fetch_log (source_id, file, rows_seen, rows_kept) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (source_id, file) DO UPDATE SET fetched_at = now(),
                        rows_seen = fetch_log.rows_seen + EXCLUDED.rows_seen, rows_kept = fetch_log.rows_kept + EXCLUDED.rows_kept""",
                     (sid, f"wiki:{hour}", len(rows), kept))
    if cursor:
        conn.execute("""INSERT INTO stream_cursor (name, cursor, updated_at) VALUES ('wikipedia', %s, now())
                        ON CONFLICT (name) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()""", (cursor,))
    conn.commit()
    return kept


def geolocate(conn, lookup=wp.geodata, limit: int = GEO_BATCH) -> int:
    """Coordinates for pages that got busy in the last 2 h and are not cached
    (or were checked over RECHECK_DAYS ago). Returns pages checked."""
    hot = conn.execute("""
        SELECT e.wiki, min(e.server), e.title
        FROM wiki_edit e
        LEFT JOIN wiki_page p ON p.wiki = e.wiki AND p.title = e.title
        WHERE e.at > now() - interval '2 hours' AND NOT e.bot
          AND (p.wiki IS NULL OR p.checked_at < now() - make_interval(days => %s))
        GROUP BY e.wiki, e.title
        HAVING count(DISTINCT e.user_hash) >= %s OR bool_or(e.created)
        LIMIT %s""", (wp.RECHECK_DAYS, wp.HOT_EDITORS, limit)).fetchall()
    by_wiki: dict[tuple[str, str], list[str]] = {}
    for wiki, server, title in hot:
        by_wiki.setdefault((wiki, server), []).append(title)
    n = 0
    for (wiki, server), titles in by_wiki.items():
        for i in range(0, len(titles), 50):
            chunk = titles[i:i + 50]
            try:
                coords = lookup(server, chunk)
            except Exception as exc:  # noqa: BLE001
                print(f"  geodata {server}: {exc}", file=sys.stderr)
                continue
            for t, c in coords.items():
                conn.execute("""
                    INSERT INTO wiki_page (wiki, title, geom, checked_at)
                    VALUES (%s, %s, CASE WHEN %s::float8 IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(%s, %s), 4326) END, now())
                    ON CONFLICT (wiki, title) DO UPDATE SET geom = EXCLUDED.geom, checked_at = now()""",
                             (wiki, t, c[1] if c else None, c[1] if c else None, c[0] if c else None))
                n += 1
    # Country for newly placed pages, as for the primary feeds.
    conn.execute("""
        UPDATE wiki_page p SET country = coalesce(
            (SELECT c.fips FROM country_shape c WHERE ST_Contains(c.geom, p.geom) LIMIT 1),
            (SELECT c.fips FROM country_shape c WHERE ST_DWithin(c.geom::geography, p.geom::geography, 100000)
              ORDER BY c.geom <-> p.geom LIMIT 1), '')
        WHERE p.geom IS NOT NULL AND p.country IS NULL""")
    conn.commit()
    return n


def run(seconds: float | None) -> None:
    end = time.monotonic() + seconds if seconds else None
    backoff = 1
    with connect() as conn:
        sid = store.source_id(conn, wp.SOURCE)
        while end is None or time.monotonic() < end:
            cursor = (conn.execute("SELECT cursor FROM stream_cursor WHERE name = 'wikipedia'").fetchone() or [None])[0]
            conn.commit()
            rows: list[tuple] = []
            last_flush = last_geo = time.monotonic()
            try:
                with wp.open_stream(cursor) as resp:
                    lines = (b.decode("utf-8", "replace") for b in resp)
                    for eid, d in wp.sse(lines):
                        if eid:
                            cursor = eid
                        if wp.keep(d):
                            rows.append(wp.row(d))
                        now = time.monotonic()
                        if now - last_flush >= FLUSH_S:
                            flush(conn, sid, rows, cursor)
                            rows, last_flush = [], now
                        if now - last_geo >= GEO_EVERY_S:
                            geolocate(conn)
                            last_geo = now
                        if end and now >= end:
                            break
                flush(conn, sid, rows, cursor)
                backoff = 1
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                print(f"stream error: {exc}; reconnecting in {backoff}s", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float)
    run(ap.parse_args().seconds)
