"""USGS earthquakes, M4.5+ over the past day, as GeoJSON. Ids are stable and
events are revised (magnitude, depth) for hours after, so ingest upserts.
`time` is when the quake happened; `updated` is USGS's last revision. Both
are epoch milliseconds."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Iterator

from . import Event

KIND = "usgs"
SOURCE = "usgs-quakes"
FEED = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"


def fetch() -> bytes:
    req = urllib.request.Request(FEED, headers={"User-Agent": "news-radar/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def parse(blob: bytes) -> Iterator[Event]:
    for f in json.loads(blob)["features"]:
        p = f["properties"]
        lon, lat, depth = (f["geometry"]["coordinates"] + [None])[:3]
        if p.get("mag") is None or lat is None or lon is None:
            continue
        t = datetime.fromtimestamp(p["time"] / 1000, tz=timezone.utc)
        yield Event(
            external_id=f["id"], occurred_on=t.date(), added_at=t,
            cameo_code=None, cameo_root=None, quad_class=None, goldstein=None, tone=None,
            actor1_name=None, actor1_country=None, actor2_name=None, actor2_country=None,
            geo_type=None, geo_name=p.get("place"), country="", adm1=None, lat=float(lat), lon=float(lon),
            num_mentions=1, num_sources=1, num_articles=1, url=p.get("url"),
            props={"kind": "earthquake", "title": p.get("title"), "mag": p["mag"], "depth_km": depth,
                   "alert": p.get("alert"), "tsunami": bool(p.get("tsunami")), "sig": p.get("sig"),
                   "updated": datetime.fromtimestamp(p["updated"] / 1000, tz=timezone.utc).isoformat() if p.get("updated") else None},
        )
