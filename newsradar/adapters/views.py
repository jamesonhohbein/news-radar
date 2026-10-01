"""VIEWS conflict forecasts (R36): predicted state-based fatalities per
country and month, 36 months ahead, from Uppsala and PRIO. No key.

The API root lists every run; a new fatalities run lands about monthly
(fatalities003_2026_08_t01 on 2026-09-30). Only the newest run is loaded,
at country-month level, state-based violence (cm/sb): 6,876 rows, which one
page of pagesize 10000 holds (1 MB, about 8 s). PRIO-GRID is ~470k rows a
run and is not loaded.

Not events: rows go to conflict_forecast keyed (run, country, month), with
the country mapped from ISO3 to FIPS through country_code. Rows that do not
map are counted in fetch_log and dropped."""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import date, datetime, timezone
from typing import Callable, Iterator

KIND = "views"
SOURCE = "views-forecast"
FEED = "https://api.viewsforecasting.org/"
_RUN = re.compile(r"^fatalities(\d+)_(\d{4})_(\d{2})_t(\d+)$")


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "news-radar/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def newest_run(runs: list[str]) -> str | None:
    """Latest by (year, month), then model version, then iteration."""
    keyed = [((int(m[2]), int(m[3]), int(m[1]), int(m[4])), r) for r in runs if (m := _RUN.match(r))]
    return max(keyed)[1] if keyed else None


def rows(pages: Iterator[dict]) -> Iterator[tuple[str, date, float, float | None]]:
    """(iso3, month, fatalities, p_any) from every page."""
    for page in pages:
        for d in page.get("data") or ():
            yield d["isoab"], date(d["year"], d["month"], 1), float(d["main_mean"]), d.get("main_dich")


def pages(run: str, fetch: Callable[[str], bytes] = _get) -> Iterator[dict]:
    url = f"{FEED}{run}/cm/sb?pagesize=10000"
    while url:
        page = json.loads(fetch(url))
        yield page
        url = page.get("next_page") or None


def load(conn, sid: int, log_fetch: Callable, fetch: Callable[[str], bytes] = _get) -> str:
    run = newest_run(json.loads(fetch(FEED))["runs"])
    if not run:
        raise RuntimeError("no fatalities run listed")
    if conn.execute("SELECT 1 FROM fetch_log WHERE source_id = %s AND file = %s", (sid, run)).fetchone():
        log_fetch(conn, sid, f"poll:{datetime.now(timezone.utc):%Y%m%d}", 0, 0)
        return f"{run} already loaded"
    got = list(rows(pages(run, fetch)))
    fips = dict(conn.execute("SELECT iso3, fips FROM country_code WHERE fips IS NOT NULL").fetchall())
    kept = [(run, fips[iso], month, f, p) for iso, month, f, p in got if iso in fips]
    with conn.cursor() as cur:
        cur.executemany("""INSERT INTO conflict_forecast (run, country, month, fatalities, p_any)
                           VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""", kept)
    log_fetch(conn, sid, run, len(got), len(kept))
    return f"{run}: {len(got)} rows, {len(kept)} kept"
