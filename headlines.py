#!/usr/bin/env python3
"""Fetch headlines for story URLs (R16). Runs after every ingest.

    headlines.py            # fetch what SELECT_SQL returns
    headlines.py --limit 50 # cap a run

Each URL is fetched once per attempt with a 10 s timeout and a 512 KB cap;
only the head of the document is needed. Failures are stored so the
selection query can space retries; see newsradar/headlines.py.
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from newsradar.db import connect
from newsradar.headlines import (MAX_ATTEMPTS, MIN_SOURCES, RETRY_AFTER_HOURS, SELECT_SQL, UPSERT_SQL,
                                 WINDOW_HOURS, extract_title, site_of)

UA = "news-radar/0.1 (+https://github.com/jamesonhohbein/news-radar)"
CAP = 512 * 1024


def fetch_title(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=10) as r:
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype and "xml" not in ctype:
            return None
        raw = r.read(CAP)
    charset = "utf-8"
    if "charset=" in ctype:
        charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    try:
        page = raw.decode(charset, "replace")
    except LookupError:
        page = raw.decode("utf-8", "replace")
    return extract_title(page)


def _one(url: str) -> dict:
    try:
        title = fetch_title(url)
        return {"url": url, "title": title, "site": site_of(url), "status": "ok" if title else "fail"}
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "title": None, "site": site_of(url), "status": "fail", "err": str(exc)[:80]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    with connect() as conn:
        urls = [r[0] for r in conn.execute(SELECT_SQL, {"window": WINDOW_HOURS, "min_sources": MIN_SOURCES,
                                                        "max_attempts": MAX_ATTEMPTS, "retry_after": RETRY_AFTER_HOURS}).fetchall()]
        urls = urls[: args.limit]
        ok = fail = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for res in pool.map(_one, urls):
                if res["status"] == "ok":
                    ok += 1
                else:
                    fail += 1
                    if res.get("err"):
                        print(f"  {res['site']}: {res['err']}", file=sys.stderr)
                conn.execute(UPSERT_SQL, res)
                conn.commit()
        print(f"headlines: {len(urls)} selected, {ok} ok, {fail} failed")


if __name__ == "__main__":
    main()
