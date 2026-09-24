#!/usr/bin/env python3
"""Consume Bluesky's Jetstream into per-place post counts (R31). Runs forever
under news-radar-bsky.service; resumes from stream_cursor (Jetstream time_us).

    bsky_stream.py                    # run
    bsky_stream.py --seconds 60       # run a minute, flush, exit (testing)
    bsky_stream.py --calibrate 900    # measure common-word place names, write gazetteer_stop

Nothing of a post is stored except, for up to URIS_PER_HOUR posts per place
per hour, its at:// URI (48 h, removed on delete)."""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import websockets

from newsradar import store
from newsradar.adapters import bluesky as bs
from newsradar.db import connect

FLUSH_S = 10
URIS_PER_HOUR = 20
TOKEN = re.compile(r"\w+")


def load_automaton(conn):
    rows = conn.execute("""
        SELECT n.place_id, n.name, p.kind, p.population, n.name = p.name
        FROM gazetteer_name n JOIN gazetteer_place p ON p.id = n.place_id
        WHERE n.name NOT IN (SELECT name FROM gazetteer_stop)""").fetchall()
    conn.commit()
    return bs.build(rows)


def bucket5(ts: datetime) -> datetime:
    return ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)


class Batch:
    def __init__(self):
        self.counts: Counter = Counter()          # (place, bucket) -> posts
        self.uris: list[tuple[str, int, datetime]] = []
        self.deletes: list[str] = []
        self.posts = 0
        self.matched = 0


def flush(conn, sid: int, b: Batch, cursor: int | None) -> None:
    """Counts, pointers, deletes and the cursor that covers them, in one transaction."""
    with conn.cursor() as cur:
        if b.counts:
            cur.execute("CREATE TEMP TABLE cstaging (place_id bigint, bucket timestamptz, posts int) ON COMMIT DROP")
            with cur.copy("COPY cstaging FROM STDIN") as copy:
                for (pid, bk), n in b.counts.items():
                    copy.write_row((pid, bk, n))
            cur.execute("""INSERT INTO chatter_5min (source, place_id, bucket, posts)
                           SELECT 'bluesky', place_id, bucket, posts FROM cstaging
                           ON CONFLICT (source, place_id, bucket) DO UPDATE SET posts = chatter_5min.posts + EXCLUDED.posts""")
        for uri, pid, at in b.uris:
            cur.execute("INSERT INTO bsky_uri (uri, place_id, at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (uri, pid, at))
        if b.deletes:
            cur.execute("DELETE FROM bsky_uri WHERE uri = ANY(%s)", (b.deletes,))
        if b.posts:
            hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
            cur.execute("""INSERT INTO fetch_log (source_id, file, rows_seen, rows_kept) VALUES (%s, %s, %s, %s)
                           ON CONFLICT (source_id, file) DO UPDATE SET fetched_at = now(),
                           rows_seen = fetch_log.rows_seen + EXCLUDED.rows_seen, rows_kept = fetch_log.rows_kept + EXCLUDED.rows_kept""",
                        (sid, f"bsky:{hour}", b.posts, b.matched))
        if cursor:
            cur.execute("""INSERT INTO stream_cursor (name, cursor, updated_at) VALUES ('bluesky', %s, now())
                           ON CONFLICT (name) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()""", (str(cursor),))
    conn.commit()


def handle(automaton, b: Batch, per_hour: Counter, msg) -> int | None:
    p = bs.parse(msg)
    if not p:
        return None
    op, t_us, uri, text = p
    if op == "delete":
        b.deletes.append(uri)
        return t_us
    at = datetime.fromtimestamp(t_us / 1e6, tz=timezone.utc)
    b.posts += 1
    places = bs.match(automaton, text)
    if places:
        b.matched += 1
    bk = bucket5(at)
    hour = at.replace(minute=0, second=0, microsecond=0)
    for pid in places:
        b.counts[(pid, bk)] += 1
        if per_hour[(pid, hour)] < URIS_PER_HOUR:
            per_hour[(pid, hour)] += 1
            b.uris.append((uri, pid, at))
    return t_us


async def run(seconds: float | None) -> None:
    end = time.monotonic() + seconds if seconds else None
    backoff = 1
    with connect() as conn:
        sid = store.source_id(conn, bs.SOURCE)
        automaton = load_automaton(conn)
        per_hour: Counter = Counter()
        while end is None or time.monotonic() < end:
            cursor = (conn.execute("SELECT cursor FROM stream_cursor WHERE name = 'bluesky'").fetchone() or [None])[0]
            conn.commit()
            url = bs.FEED + (f"&cursor={cursor}" if cursor else "")
            b, last, t_us = Batch(), time.monotonic(), None
            try:
                async with websockets.connect(url, max_size=2 ** 22, ping_interval=30) as ws:
                    async for msg in ws:
                        t_us = handle(automaton, b, per_hour, msg) or t_us
                        now = time.monotonic()
                        if now - last >= FLUSH_S:
                            flush(conn, sid, b, t_us)
                            b, last = Batch(), now
                            if len(per_hour) > 200_000:
                                per_hour.clear()
                        if end and now >= end:
                            break
                flush(conn, sid, b, t_us)
                backoff = 1
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                print(f"stream error: {exc}; reconnecting in {backoff}s", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


async def calibrate(seconds: float) -> None:
    """Count each single-word, capitalized gazetteer name as written and in
    lowercase; a name whose lowercase form is at least as common is a word."""
    with connect() as conn:
        names = {r[0] for r in conn.execute("SELECT DISTINCT name FROM gazetteer_name").fetchall() if bs.usable(r[0])}
        conn.commit()
        single = {n for n in names if n.isalpha() and n[0].isupper()}
        lower: dict[str, set[str]] = {}
        for n in single:   # "But" and "BUT" share "but"; both count it
            lower.setdefault(n.lower(), set()).add(n)
        cap: Counter = Counter()
        low: Counter = Counter()
        end = time.monotonic() + seconds
        posts = 0
        async with websockets.connect(bs.FEED, max_size=2 ** 22) as ws:
            async for msg in ws:
                p = bs.parse(msg)
                if p and p[0] == "create":
                    posts += 1
                    for tok in TOKEN.findall(p[3]):
                        if tok in single:
                            cap[tok] += 1
                        elif tok in lower:
                            for n in lower[tok]:
                                low[n] += 1
                if time.monotonic() >= end:
                    break
        stop = {n for n in set(cap) | set(low) if low[n] >= max(cap[n], 1) and low[n] + cap[n] >= 3}
        conn.execute("DELETE FROM gazetteer_stop")
        for n in stop:
            conn.execute("INSERT INTO gazetteer_stop VALUES (%s, %s, %s, now())", (n, cap[n], low[n]))
        conn.commit()
    print(f"calibrate: {posts} posts, {len(stop)} stop names")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--calibrate", type=float, metavar="SECONDS")
    a = ap.parse_args()
    asyncio.run(calibrate(a.calibrate) if a.calibrate else run(a.seconds))
