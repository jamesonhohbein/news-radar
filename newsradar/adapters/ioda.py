"""IODA internet outages (R28), Georgia Tech's Internet Outage Detection and
Analysis. No key. **Data is "Copyright Georgia Tech Research Corporation.
All Rights Reserved"** (in every response): ingested for personal use only,
never republished.

Events come per entity: ASN, geo-ASN, region and country. Only country and
region are kept; ASN rows are most of the volume and name networks, not
places. There are no ids: an outage is keyed (datasource, entity, start),
and its duration grows in place while it lasts. Entities have no points and
use ISO codes; `resolve` maps a region to its country through a cached
entity lookup, ISO to FIPS through `country_code`, and places the outage at
its country's point."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from . import Event

KIND = "ioda"
SOURCE = "ioda-outages"
BASE = "https://api.ioda.inetintel.cc.gatech.edu/v2"
# Every outage overlapping the window comes back, so a long one keeps growing
# in place on every run. A 7-day window came back truncated and weighted to
# old starts (measured 2026-09-24: nothing from the last 48 h), so keep it
# short; the 15-minute cadence leaves no gap.
WINDOW_HOURS = 6


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "news-radar/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch() -> bytes:
    now = int(time.time())
    return _get(f"{BASE}/outages/events?from={now - WINDOW_HOURS * 3600}&until={now}&limit=5000")


def parse(blob: bytes) -> Iterator[Event]:
    for e in json.loads(blob).get("data", []):
        kind, _, code = (e.get("location") or "").partition("/")
        if kind not in ("country", "region") or not code or e.get("start") is None:
            continue
        start = datetime.fromtimestamp(e["start"], tz=timezone.utc)
        end = start + timedelta(seconds=e.get("duration") or 0)
        yield Event(
            external_id=f"{e.get('datasource')}:{e['location']}:{e['start']}", occurred_on=start.date(), added_at=start,
            cameo_code=None, cameo_root=None, quad_class=None, goldstein=None, tone=None,
            actor1_name=None, actor1_country=None, actor2_name=None, actor2_country=None,
            geo_type=None, geo_name=e.get("location_name"), country="", adm1=None,
            lat=None, lon=None,  # type: ignore[arg-type]  # placed by resolve()
            num_mentions=1, num_sources=1, num_articles=1,
            url=f"https://ioda.inetintel.cc.gatech.edu/{kind}/{code}",
            props={"kind": "internet outage", "title": f"Internet outage: {e.get('location_name')}",
                   "entity_type": kind, "entity_code": code, "datasource": e.get("datasource"),
                   "duration_s": e.get("duration"), "end": end.isoformat(), "score": e.get("score"),
                   "method": e.get("method")},
        )


def _region_country(conn, codes: set[str], get: Callable[[str], bytes], pace: float) -> None:
    have = {r[0] for r in conn.execute("SELECT code FROM ioda_region WHERE code = ANY(%s)", (list(codes),)).fetchall()}
    for code in sorted(codes - have):
        d = None
        for i in range(3):  # the local resolver drops lookups under a burst
            try:
                d = json.loads(get(f"{BASE}/entities/query?entityType=region&entityCode={code}"))["data"]
                break
            except Exception as exc:  # noqa: BLE001
                if i == 2:
                    print(f"  ioda region {code}: {exc}", file=sys.stderr)
                elif pace:
                    time.sleep(2 ** i)
        if d is None:
            continue
        a = (d[0].get("attrs") or {}) if d else {}
        conn.execute("""INSERT INTO ioda_region (code, name, country_iso, ne_region_id) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (code) DO NOTHING""",
                     (code, d[0].get("name") if d else None, a.get("country_code"), a.get("ne_region_id")))
        time.sleep(pace)


def resolve(conn, events: list[Event], get: Callable[[str], bytes] = _get, pace: float = 0.2) -> list[Event]:
    _region_country(conn, {e.props["entity_code"] for e in events if e.props["entity_type"] == "region"}, get, pace)
    out = []
    for e in events:
        code = e.props["entity_code"]
        row = conn.execute("""
            SELECT c.fips, ST_Y(p), ST_X(p), r.name
            FROM (SELECT CASE WHEN %(t)s = 'country' THEN %(c)s
                              ELSE (SELECT country_iso FROM ioda_region WHERE code = %(c)s) END AS iso) x
            JOIN country_code c ON c.iso2 = x.iso
            JOIN LATERAL (SELECT ST_PointOnSurface(geom) p FROM country_shape s WHERE s.fips = c.fips) g ON true
            LEFT JOIN ioda_region r ON %(t)s = 'region' AND r.code = %(c)s""",
                           {"t": e.props["entity_type"], "c": code}).fetchone()
        if not row:
            continue  # unknown ISO code, or a country with no 110m polygon
        out.append(replace(e, country=row[0], lat=row[1], lon=row[2],
                           props={**e.props, "region_name": row[3]} if row[3] else e.props))
    return out
