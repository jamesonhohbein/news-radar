"""NWS active alerts (R24), US-wide, as GeoJSON CAP. Public domain; a
User-Agent is required (an empty one gets 403).

93% of alerts carry no geometry, only zone references (`affectedZones`,
`geocode.UGC`), so parse leaves the point empty and `resolve` places each
alert on its polygon, or on the union of its zones from the `nws_zone` cache,
fetching zones it has not seen. An Update or Cancel is a new id whose
`references` name what it supersedes; nothing is edited in place, so
`after_insert` marks the chain rather than overwriting."""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import replace
from datetime import datetime
from typing import Callable, Iterable, Iterator

from . import Event

KIND = "nws"
SOURCE = "nws-alerts"
FEED = "https://api.weather.gov/alerts/active"
UA = "news-radar/0.1 (github.com/jamesonhohbein/news-radar)"
ZONE_MAX_AGE_DAYS = 90


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def fetch() -> bytes:
    return _get(FEED)


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def parse(blob: bytes) -> Iterator[Event]:
    for f in json.loads(blob)["features"]:
        p = f["properties"]
        sent = _ts(p.get("sent"))
        if not p.get("id") or not sent:
            continue
        start = _ts(p.get("onset")) or _ts(p.get("effective")) or sent
        yield Event(
            external_id=p["id"], occurred_on=start.date(), added_at=sent,
            cameo_code=None, cameo_root=None, quad_class=None, goldstein=None, tone=None,
            actor1_name=None, actor1_country=None, actor2_name=None, actor2_country=None,
            geo_type=None, geo_name=(p.get("areaDesc") or "")[:300] or None, country="", adm1=None,
            lat=None, lon=None,  # type: ignore[arg-type]  # placed by resolve()
            num_mentions=1, num_sources=1, num_articles=1, url=p.get("@id"),
            props={"kind": "weather alert", "title": p.get("headline") or p.get("event"),
                   "event": p.get("event"), "severity": p.get("severity"), "urgency": p.get("urgency"),
                   "certainty": p.get("certainty"), "message_type": p.get("messageType"),
                   "expires": p.get("expires"), "ends": p.get("ends"), "sender": p.get("senderName"),
                   "ugc": (p.get("geocode") or {}).get("UGC", []),
                   "zones": p.get("affectedZones", []),
                   "references": [r["identifier"] for r in p.get("references", [])],
                   "_geometry": f.get("geometry")},
        )


def ensure_zones(conn, urls: Iterable[str], get: Callable[[str], bytes] = _get, pace: float = 0.2) -> int:
    """Fetch and cache zone polygons not seen in ZONE_MAX_AGE_DAYS. A zone with
    no geometry is cached as such, so it is not asked for every run."""
    urls = sorted(set(urls))
    fresh = {r[0] for r in conn.execute(
        "SELECT url FROM nws_zone WHERE url = ANY(%s) AND fetched_at > now() - make_interval(days => %s)",
        (urls, ZONE_MAX_AGE_DAYS)).fetchall()}
    n = 0
    for url in urls:
        if url in fresh:
            continue
        z = None
        # The local resolver drops lookups under a burst ("Temporary failure
        # in name resolution"), as in ingest.py; a short backoff clears it.
        for i in range(3):
            try:
                z = json.loads(get(url))
                break
            except Exception as exc:  # noqa: BLE001
                if i == 2:
                    print(f"  nws zone {url}: {exc}", file=sys.stderr)
                else:
                    time.sleep(2 ** i if pace else 0)
        if z is None:
            continue
        zp = z.get("properties", {})
        conn.execute("""
            INSERT INTO nws_zone (url, code, kind, name, geom, fetched_at)
            VALUES (%s, %s, %s, %s,
                    CASE WHEN %s::text IS NULL THEN NULL
                         ELSE ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)), 3)) END,
                    now())
            ON CONFLICT (url) DO UPDATE SET code = EXCLUDED.code, kind = EXCLUDED.kind, name = EXCLUDED.name,
                                            geom = EXCLUDED.geom, fetched_at = now()""",
                     (url, zp.get("id") or url.rsplit("/", 1)[1], zp.get("type") or url.rsplit("/", 2)[1],
                      zp.get("name"), json.dumps(z["geometry"]) if z.get("geometry") else None,
                      json.dumps(z["geometry"]) if z.get("geometry") else None))
        n += 1
        time.sleep(pace)
    return n


def resolve(conn, events: list[Event], get: Callable[[str], bytes] = _get, pace: float = 0.2) -> list[Event]:
    """A point for every alert: on its own polygon if it has one, else on the
    union of its cached zones. Alerts that cannot be placed are dropped."""
    ensure_zones(conn, (z for e in events if not e.props["_geometry"] for z in e.props["zones"]), get, pace)
    out = []
    for e in events:
        geom = e.props["_geometry"]
        if geom:
            row = conn.execute("SELECT ST_Y(p), ST_X(p) FROM (SELECT ST_PointOnSurface(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))) p) x",
                               (json.dumps(geom),)).fetchone()
        else:
            row = conn.execute("SELECT ST_Y(p), ST_X(p) FROM (SELECT ST_PointOnSurface(ST_Union(geom)) p FROM nws_zone WHERE url = ANY(%s) AND geom IS NOT NULL) x",
                               (e.props["zones"],)).fetchone()
        props = {k: v for k, v in e.props.items() if k != "_geometry"}
        if row and row[0] is not None:
            out.append(replace(e, lat=row[0], lon=row[1], props=props))
    return out


def after_insert(conn, sid: int) -> int:
    """Mark every alert that a later message references as superseded by it."""
    return conn.execute("""
        UPDATE event o SET superseded_by = n.external_id
        FROM event n, jsonb_array_elements_text(n.props->'references') r(ref)
        WHERE n.source_id = %s AND o.source_id = %s AND o.external_id = r.ref
          AND o.superseded_by IS DISTINCT FROM n.external_id""", (sid, sid)).rowcount
