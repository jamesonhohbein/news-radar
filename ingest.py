#!/usr/bin/env python3
"""Pull source files into the event table.

    ingest.py latest              # the newest GDELT export, if not already loaded
    ingest.py backfill --days 30  # everything in the master list since then
    ingest.py catchup             # files since the last one logged (what the timer runs)
    ingest.py primary             # the feed adapters (USGS, GDACS): fetch, upsert, geocode
    ingest.py backfill --days 30 --mentions   # Mentions files only (R18)

catchup loads Mentions after Events for the same window, so a mention's event
is already in the table when its row arrives.

Every path is idempotent: fetch_log skips files, the event unique key skips
rows. Downloads run in a small pool; inserts are sequential in one
transaction per file so a crash leaves whole files, never half of one.
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from newsradar import store
from newsradar.adapters import gdacs, gdelt, nws, usgs
from newsradar.db import connect

SOURCE = "gdelt-events"
MENTIONS_SOURCE = "gdelt-mentions"
GKG_SOURCE = "gdelt-gkg"
FEEDS = (usgs, gdacs, nws)


def load_files(conn, sid: int, files: list[str], workers: int = 4) -> tuple[int, int]:
    done = store.already_fetched(conn, sid, files)
    todo = [f for f in files if f not in done]
    kept_total = inserted_total = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for file, blob in zip(todo, pool.map(_fetch_or_none, todo)):
            if blob is None:
                print(f"  {file}: fetch failed, skipped", file=sys.stderr)
                continue
            seen = kept = 0
            def events():
                nonlocal seen, kept
                for ev, ok in gdelt.parse(file, blob):
                    seen += 1
                    if ok:
                        kept += 1
                        yield ev
            # Explicit commit per file. conn.transaction() would be a savepoint
            # here (a SELECT has already opened the transaction), and the
            # staging table's ON COMMIT DROP only fires on a real commit.
            try:
                inserted = store.insert_events(conn, sid, events())
                store.log_fetch(conn, sid, file, seen, kept)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            kept_total += kept
            inserted_total += inserted
    return len(todo), inserted_total


def load_mention_files(conn, msid: int, events_sid: int, files: list[str], workers: int = 4) -> tuple[int, int]:
    """Mentions files into the raw buffer, one commit per file."""
    done = store.already_fetched(conn, msid, files)
    todo = [f for f in files if f not in done]
    kept_total = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for file, blob in zip(todo, pool.map(_fetch_or_none, todo)):
            if blob is None:
                print(f"  {file}: fetch failed, skipped", file=sys.stderr)
                continue
            bad = 0
            def rows():
                nonlocal bad
                for r in gdelt.parse_mentions(blob):
                    if r is None:
                        bad += 1
                    else:
                        yield r
            try:
                seen, kept = store.insert_mentions(conn, events_sid, rows())
                store.log_fetch(conn, msid, file, seen + bad, kept)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            kept_total += kept
    return len(todo), kept_total


def load_gkg_files(conn, gsid: int, files: list[str], workers: int = 4) -> tuple[int, int]:
    """GKG files into gkg_article/gkg_location and the theme tables, one
    commit per file so the additive theme counts match fetch_log exactly."""
    done = store.already_fetched(conn, gsid, files)
    todo = [f for f in files if f not in done]
    kept_total = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for file, blob in zip(todo, pool.map(_fetch_or_none, todo)):
            if blob is None:
                print(f"  {file}: fetch failed, skipped", file=sys.stderr)
                continue
            parsed = list(gdelt.parse_gkg(blob))
            good = [a for a in parsed if a is not None]
            try:
                kept = store.insert_gkg(conn, good)
                store.log_fetch(conn, gsid, file, len(parsed), kept)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            kept_total += kept
    return len(todo), kept_total


def _since_last(conn, sid: int) -> datetime:
    # Six hours covers a sleep or a GDELT outage; beyond that use backfill.
    row = conn.execute("SELECT max(file) FROM fetch_log WHERE source_id = %s", (sid,)).fetchone()
    floor = datetime.now(timezone.utc) - timedelta(hours=6)
    return max(gdelt.file_stamp(row[0]), floor) if row[0] else floor


def _fetch_or_none(file: str, attempts: int = 4) -> bytes | None:
    # The local resolver drops lookups under a burst of parallel fetches
    # ("Temporary failure in name resolution"); a short backoff clears it.
    for i in range(attempts):
        try:
            return gdelt.fetch(file)
        except Exception as exc:  # noqa: BLE001
            if i == attempts - 1:
                print(f"  {file}: {exc}", file=sys.stderr)
                return None
            time.sleep(2 ** i)


def load_feeds(conn) -> list[str]:
    """Each feed is one endpoint holding current state; the whole thing is
    fetched and upserted every run. fetch_log gets one row per run, keyed by
    kind and minute, so source_health can see the feed is alive."""
    out = []
    for mod in FEEDS:
        try:
            sid = store.source_id(conn, mod.SOURCE)
        except SystemExit:
            out.append(f"{mod.KIND}: disabled")
            continue
        try:
            blob = mod.fetch()
        except Exception as exc:  # noqa: BLE001
            out.append(f"{mod.KIND}: fetch failed: {exc}")
            continue
        events = list(mod.parse(blob))
        try:
            # Optional hooks: resolve places events that arrive without a point
            # (NWS zones); after_insert links rows to each other (NWS supersession).
            if hasattr(mod, "resolve"):
                events = mod.resolve(conn, events)
            n = store.insert_events(conn, sid, events, update=True)
            if hasattr(mod, "after_insert"):
                mod.after_insert(conn, sid)
            geocoded = store.reverse_geocode(conn, sid)
            store.log_fetch(conn, sid, f"{mod.KIND}:{datetime.now(timezone.utc):%Y%m%d%H%M}", len(events), len(events))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        out.append(f"{mod.KIND}: {len(events)} events, {n} upserted, {geocoded} geocoded")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("latest")
    sub.add_parser("catchup")
    sub.add_parser("primary")
    b = sub.add_parser("backfill")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--mentions", action="store_true", help="Mentions files instead of Events")
    args = ap.parse_args()

    with connect() as conn:
        if args.cmd == "primary":
            for line in load_feeds(conn):
                print(line)
            return
        sid = store.source_id(conn, SOURCE)
        msid = store.source_id(conn, MENTIONS_SOURCE)
        if args.cmd == "backfill" and args.mentions:
            files = gdelt.list_files(datetime.now(timezone.utc) - timedelta(days=args.days), gdelt.MENTIONS)
            n_files, n_rows = load_mention_files(conn, msid, sid, files)
            print(f"backfill: {n_files} mentions files, {n_rows} mentions kept")
            return
        if args.cmd == "latest":
            files = [gdelt.latest()]
        elif args.cmd == "backfill":
            files = gdelt.list_files(datetime.now(timezone.utc) - timedelta(days=args.days))
        else:
            files = gdelt.list_files(_since_last(conn, sid))
        n_files, n_rows = load_files(conn, sid, files)
        print(f"{args.cmd}: {n_files} new files, {n_rows} events inserted")
        if args.cmd == "catchup":
            mfiles = gdelt.list_files(_since_last(conn, msid), gdelt.MENTIONS)
            n_files, n_rows = load_mention_files(conn, msid, sid, mfiles)
            print(f"catchup: {n_files} mentions files, {n_rows} mentions kept")
            gsid = store.source_id(conn, GKG_SOURCE)
            gfiles = gdelt.list_files(_since_last(conn, gsid), gdelt.GKG)
            n_files, n_rows = load_gkg_files(conn, gsid, gfiles)
            print(f"catchup: {n_files} gkg files, {n_rows} articles kept")


if __name__ == "__main__":
    main()
