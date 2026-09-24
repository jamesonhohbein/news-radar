"""Cloudflare Radar outage annotations (R29): curated outages with a cause
(power, cable cut, government-directed, weather...) and a type (nationwide,
regional, network). Needs `CLOUDFLARE_RADAR_TOKEN` in .env, a free account
token with Radar Read; without it the adapter is skipped. Data is
CC BY-NC 4.0.

Overlaps IODA and is kept because it names the cause. An annotation lists
ISO2 locations; each becomes one event, placed at its country's point
(ISO -> FIPS through `country_code`). `endDate` null means ongoing.
Response schema from Cloudflare's API reference, not yet seen live."""
from __future__ import annotations

import json
import urllib.request
from dataclasses import replace
from datetime import datetime
from typing import Iterator

from ..db import load_env
from . import Event

KIND = "radar"
SOURCE = "cloudflare-radar"
FEED = "https://api.cloudflare.com/client/v4/radar/annotations/outages"


class NotConfigured(Exception):
    pass


def fetch() -> bytes:
    token = load_env().get("CLOUDFLARE_RADAR_TOKEN")
    if not token:
        raise NotConfigured("no CLOUDFLARE_RADAR_TOKEN in .env")
    req = urllib.request.Request(f"{FEED}?dateRange=7d&limit=500&format=json",
                                 headers={"Authorization": f"Bearer {token}", "User-Agent": "news-radar/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def parse(blob: bytes) -> Iterator[Event]:
    for a in json.loads(blob).get("result", {}).get("annotations", []):
        start = _ts(a.get("startDate"))
        if not a.get("id") or not start:
            continue
        o = a.get("outage") or {}
        names = {d["code"]: d.get("name") for d in a.get("locationsDetails") or []}
        for loc in a.get("locations") or []:
            yield Event(
                external_id=f"{a['id']}:{loc}", occurred_on=start.date(), added_at=start,
                cameo_code=None, cameo_root=None, quad_class=None, goldstein=None, tone=None,
                actor1_name=None, actor1_country=None, actor2_name=None, actor2_country=None,
                geo_type=None, geo_name=a.get("scope") or names.get(loc), country="", adm1=None,
                lat=None, lon=None,  # type: ignore[arg-type]  # placed by resolve()
                num_mentions=1, num_sources=1, num_articles=1, url=a.get("linkedUrl") or None,
                props={"kind": "internet outage", "title": a.get("description"), "iso2": loc,
                       "cause": o.get("outageCause"), "outage_type": o.get("outageType"),
                       "scope": a.get("scope"), "end": a.get("endDate"), "data_source": a.get("dataSource"),
                       "asns": [d.get("name") for d in a.get("asnsDetails") or []]},
            )


def resolve(conn, events: list[Event]) -> list[Event]:
    out = []
    for e in events:
        row = conn.execute("""SELECT c.fips, ST_Y(ST_PointOnSurface(s.geom)), ST_X(ST_PointOnSurface(s.geom))
                              FROM country_code c JOIN country_shape s ON s.fips = c.fips WHERE c.iso2 = %s""",
                           (e.props["iso2"],)).fetchone()
        if row:
            out.append(replace(e, country=row[0], lat=row[1], lon=row[2]))
    return out
