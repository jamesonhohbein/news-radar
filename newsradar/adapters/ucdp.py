"""UCDP candidate events (R35): researcher-coded organised violence with
fatality estimates, about a month behind. CC BY 4.0, no key.

The UCDP API needs an access token (401 without one, 2026-09-30), so this
reads the CSVs on the public downloads page instead. Two release shapes sit
there: a monthly file (GEDEvent_v26_0_8.csv, August) and a consolidated one
covering several months (GEDEvent_v26_01_26_06.csv, January to June). Event
ids are stable across releases, so a later file upserts and revises deaths,
coding status and place in place. Files load oldest first so the latest
release wins.

UCDP publishes no per-event timestamp, so added_at is when we first fetched
a file carrying the event, and a revision keeps it."""
from __future__ import annotations

import csv
import io
import re
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Iterator

from . import Event

KIND = "ucdp"
SOURCE = "ucdp-candidate"
FEED = "https://ucdp.uu.se/downloads/"
_LINK = re.compile(r'href="(https://ucdp\.uu\.se/downloads/candidateged/(GEDEvent_v(\d+)_(\d+)_(\d+)(?:_(\d+))?\.csv))"', re.I)


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "news-radar/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def candidate_files(html: str) -> list[tuple[str, str]]:
    """(url, file name) for every candidate CSV, oldest release first.
    v26_0_8 is month 8; v26_01_26_06 ends at month 6. The final GED (ged261)
    and the VPP files are other datasets and never match."""
    found = {}
    for url, name, year, a, b, c in _LINK.findall(html):
        end = int(c) if c else int(b)  # consolidated: last month; monthly: the month
        found[name] = (url, name, (int(year), end, 0 if c else 1))
    return [(u, n) for u, n, _ in sorted(found.values(), key=lambda t: t[2])]


def _int(s: str | None) -> int | None:
    return int(s) if s not in (None, "") else None


def _day(s: str):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def parse(blob: bytes, added_at: datetime) -> Iterator[Event]:
    for r in csv.DictReader(io.StringIO(blob.decode("utf-8-sig"))):
        if not r.get("id") or r.get("latitude") in (None, "") or r.get("longitude") in (None, ""):
            continue
        best = _int(r.get("best")) or 0
        yield Event(
            external_id=r["id"], occurred_on=_day(r["date_start"]), added_at=added_at,
            cameo_code=None, cameo_root=None, quad_class=None, goldstein=None, tone=None,
            actor1_name=r.get("side_a") or None, actor1_country=None,
            actor2_name=r.get("side_b") or None, actor2_country=None,
            geo_type=None, geo_name=r.get("where_coordinates") or None, country="", adm1=None,
            lat=float(r["latitude"]), lon=float(r["longitude"]),
            num_mentions=1, num_sources=_int(r.get("number_of_sources")) or 1, num_articles=1, url=None,
            props={"kind": "ucdp", "title": f"{r.get('dyad_name')}: {best} killed",
                   "type_of_violence": _int(r.get("type_of_violence")),
                   "conflict_name": r.get("conflict_name"), "dyad_name": r.get("dyad_name"),
                   "side_a": r.get("side_a"), "side_b": r.get("side_b"),
                   "best": best, "low": _int(r.get("low")), "high": _int(r.get("high")),
                   "deaths_civilians": _int(r.get("deaths_civilians")),
                   "where_prec": _int(r.get("where_prec")), "date_prec": _int(r.get("date_prec")),
                   "date_end": str(_day(r["date_end"])) if r.get("date_end") else None,
                   "code_status": r.get("code_status"),
                   "source_office": r.get("source_office"), "source_headline": r.get("source_headline")},
        )


def load(conn, sid: int, log_fetch: Callable, fetch: Callable[[str], bytes] = _get) -> str:
    """Every candidate file not yet in fetch_log, oldest first, one commit
    per file (insert_events' staging table only drops on a real commit)."""
    from .. import store
    files = candidate_files(fetch(FEED).decode("utf-8", "replace"))
    done = store.already_fetched(conn, sid, [n for _, n in files])
    loaded = upserted = 0
    for url, name in files:
        if name in done:
            continue
        events = list(parse(fetch(url), datetime.now(timezone.utc)))
        upserted += store.insert_events(conn, sid, events, update=True, keep=("added_at",))
        log_fetch(conn, sid, name, len(events), len(events))
        conn.commit()
        loaded += 1
    if not loaded:
        # A run with nothing new still shows the poll is alive.
        log_fetch(conn, sid, f"poll:{datetime.now(timezone.utc):%Y%m%d}", 0, 0)
    return f"{len(files)} files listed, {loaded} new, {upserted} events upserted"
