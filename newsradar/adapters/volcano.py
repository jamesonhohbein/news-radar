"""USGS volcanoes at elevated status (R26), from the Volcano Science
Center's API. Public domain, no key.

The endpoint lists only volcanoes above Normal/Green, each with its point,
colour code, the previous code and when it changed, and the latest notice.
A colour change is an event: the row is keyed on (vnum, codeChangeDate), so
a new code makes a new row and notices at the same level update `props`.
A volcano that returns to Green drops out of the list; `props.seen` is the
fetch time, so `primary_live` treats a row not seen lately as no longer
elevated."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Iterator

from . import Event

KIND = "volcano"
SOURCE = "usgs-volcanoes"
FEED = "https://volcanoes.usgs.gov/vsc/api/volcanoApi/elevated"


def fetch() -> bytes:
    req = urllib.request.Request(FEED, headers={"User-Agent": "news-radar/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _utc(s: str | None) -> datetime | None:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if s else None


def parse(blob: bytes, now: datetime | None = None) -> Iterator[Event]:
    seen = (now or datetime.now(timezone.utc)).isoformat()
    for v in json.loads(blob):
        changed = _utc(v.get("codeChangeDate")) or _utc(v.get("sentUtc"))
        if not v.get("vnum") or v.get("lat") is None or v.get("long") is None or not changed:
            continue
        yield Event(
            external_id=f"{v['vnum']}:{changed:%Y%m%d%H%M%S}", occurred_on=changed.date(), added_at=changed,
            cameo_code=None, cameo_root=None, quad_class=None, goldstein=None, tone=None,
            actor1_name=None, actor1_country=None, actor2_name=None, actor2_country=None,
            geo_type=None, geo_name=v.get("vName"), country="", adm1=None,
            lat=float(v["lat"]), lon=float(v["long"]),
            num_mentions=1, num_sources=1, num_articles=1, url=v.get("noticeUrl"),
            props={"kind": "volcano", "title": f"{v.get('vName')} {v.get('colorCode')}/{v.get('alertLevel')}",
                   "color": v.get("colorCode"), "alert": v.get("alertLevel"),
                   "color_prev": v.get("colorCodePrev"), "alert_prev": v.get("alertLevelPrev"),
                   "threat": v.get("nvewsThreat"), "observatory": v.get("obs"),
                   "synopsis": v.get("noticeSynopsis"), "notice_id": v.get("noticeId"),
                   "notice_sent": (_utc(v.get("sentUtc")) or changed).isoformat(), "seen": seen},
        )
