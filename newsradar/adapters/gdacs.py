"""GDACS disaster alerts (EQ, TC, FL, VO, DR, WF) as RSS with georss points.
One item per event episode; `eventid` is stable and `alertlevel` moves
Green -> Orange -> Red as an event develops, so ingest upserts. `dateadded`
is when GDACS first published it; `fromdate` is the event's own start, which
for a forecast flood can be in the future and is stored as occurred_on."""
from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterator

from . import Event

KIND = "gdacs"
SOURCE = "gdacs-alerts"
FEED = "https://www.gdacs.org/xml/rss.xml"
NS = {"gdacs": "http://www.gdacs.org", "georss": "http://www.georss.org/georss"}
TYPES = {"EQ": "earthquake", "TC": "tropical cyclone", "FL": "flood", "VO": "volcano", "DR": "drought", "WF": "wildfire", "TS": "tsunami"}


def fetch() -> bytes:
    req = urllib.request.Request(FEED, headers={"User-Agent": "news-radar/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse(blob: bytes) -> Iterator[Event]:
    root = ET.fromstring(blob)
    for it in root.iter("item"):
        g = lambda tag, ns="gdacs": (it.findtext(f"{ns}:{tag}", namespaces=NS) or "").strip()  # noqa: E731
        point = g("point", "georss").split()
        etype, eid = g("eventtype"), g("eventid")
        if len(point) != 2 or not etype or not eid:
            continue
        lat, lon = float(point[0]), float(point[1])
        added = _date(g("dateadded")) or _date(it.findtext("pubDate"))
        if not added:
            continue
        start = _date(g("fromdate"))
        sev = it.find("gdacs:severity", NS)
        pop = it.find("gdacs:population", NS)
        yield Event(
            external_id=f"{etype}{eid}", occurred_on=(start or added).date(), added_at=added,
            cameo_code=None, cameo_root=None, quad_class=None, goldstein=None, tone=None,
            actor1_name=None, actor1_country=None, actor2_name=None, actor2_country=None,
            geo_type=None, geo_name=g("country") or None, country="", adm1=None, lat=lat, lon=lon,
            num_mentions=1, num_sources=1, num_articles=1, url=(it.findtext("link") or "").strip() or None,
            props={"kind": TYPES.get(etype, etype), "event_type": etype, "title": (it.findtext("title") or "").strip(),
                   "alert": g("alertlevel"), "alert_score": g("alertscore"), "episode": g("episodeid"),
                   "iso3": g("iso3"), "severity": (sev.text or "").strip() if sev is not None else None,
                   "severity_value": sev.get("value") if sev is not None else None,
                   "population": (pop.text or "").strip() if pop is not None else None,
                   "current": g("iscurrent") == "true", "modified": (_date(g("datemodified")) or added).isoformat()},
        )
