#!/usr/bin/env python3
"""Pull source files into the event table.

    ingest.py latest              # the newest GDELT export, if not already loaded
    ingest.py backfill --days 30  # everything in the master list since then
    ingest.py catchup             # files since the last one logged (what the timer runs)

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
from newsradar.adapters import gdelt
from newsradar.db import connect

SOURCE = "gdelt-events"


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


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("latest")
    sub.add_parser("catchup")
    b = sub.add_parser("backfill")
    b.add_argument("--days", type=int, default=30)
    args = ap.parse_args()

    with connect() as conn:
        sid = store.source_id(conn, SOURCE)
        if args.cmd == "latest":
            files = [gdelt.latest()]
        elif args.cmd == "backfill":
            files = gdelt.list_files(datetime.now(timezone.utc) - timedelta(days=args.days))
        else:
            row = conn.execute("SELECT max(file) FROM fetch_log WHERE source_id = %s", (sid,)).fetchone()
            since = gdelt.file_stamp(row[0]) if row[0] else datetime.now(timezone.utc) - timedelta(hours=6)
            # Six hours covers a sleep or a GDELT outage; beyond that use backfill.
            files = gdelt.list_files(max(since, datetime.now(timezone.utc) - timedelta(hours=6)))
        n_files, n_rows = load_files(conn, sid, files)
        print(f"{args.cmd}: {n_files} new files, {n_rows} events inserted")


if __name__ == "__main__":
    main()
