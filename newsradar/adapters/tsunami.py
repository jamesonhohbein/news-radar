"""Tsunami bulletins (R25) from the two US warning centres, as Atom with a
geo point per entry. Public domain, no key.

Each feed holds only its latest bulletin, so the history is built by polling
and keeping every new entry id (`urn:uuid`, one per bulletin). PAAQ is the
National Tsunami Warning Center in Palmer, which covers the US West Coast,
British Columbia and Alaska; PHEB is the Pacific centre in Honolulu. The
category (Information, Advisory, Watch, Warning, Threat) sits only in the
summary's XHTML, so it is read from there."""
from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Iterator

from . import Event

KIND = "tsunami"
SOURCE = "tsunami-bulletins"
BASE = "https://www.tsunami.gov/events/xml/"
CENTRES = ("PAAQ", "PHEB")
NS = {"a": "http://www.w3.org/2005/Atom", "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#"}
SEP = b"\n<!-- news-radar feed boundary -->\n"


def fetch() -> bytes:
    out = []
    for c in CENTRES:
        req = urllib.request.Request(f"{BASE}{c}Atom.xml", headers={"User-Agent": "news-radar/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out.append(r.read())
    return SEP.join(out)


def _field(text: str, label: str) -> str | None:
    m = re.search(rf"{label}:\s*</(?:[\w.]+:)?(?:strong|b)>\s*([^<]*)", text)
    return m.group(1).strip() or None if m else None


def parse(blob: bytes) -> Iterator[Event]:
    for part, centre in zip(blob.split(SEP), CENTRES):
        root = ET.fromstring(part)
        author = (root.findtext("a:author/a:name", namespaces=NS) or "").strip()
        for e in root.findall("a:entry", NS):
            uid = (e.findtext("a:id", namespaces=NS) or "").strip()
            lat, lon = e.findtext("geo:lat", namespaces=NS), e.findtext("geo:long", namespaces=NS)
            updated = e.findtext("a:updated", namespaces=NS)
            if not uid or not lat or not lon or not updated:
                continue
            summary = ET.tostring(e.find("a:summary", NS), encoding="unicode") if e.find("a:summary", NS) is not None else ""
            mag = _field(summary, "Preliminary Magnitude")
            cap = next((l.get("href") for l in e.findall("a:link", NS) if l.get("title") == "CapXML document"), None)
            bulletin = next((l.get("href") for l in e.findall("a:link", NS) if l.get("title") == "Bulletin"), None)
            t = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            region = (e.findtext("a:title", namespaces=NS) or "").strip()
            yield Event(
                external_id=uid, occurred_on=t.date(), added_at=t,
                cameo_code=None, cameo_root=None, quad_class=None, goldstein=None, tone=None,
                actor1_name=None, actor1_country=None, actor2_name=None, actor2_country=None,
                geo_type=None, geo_name=region or None, country="", adm1=None, lat=float(lat), lon=float(lon),
                num_mentions=1, num_sources=1, num_articles=1, url=bulletin,
                props={"kind": "tsunami bulletin", "centre": centre, "issuer": author,
                       "category": _field(summary, "Category"),
                       "title": " ".join((root.findtext("a:title", namespaces=NS) or "").split()),
                       "magnitude": float(re.match(r"[\d.]+", mag).group()) if mag and re.match(r"[\d.]+", mag) else None,
                       "magnitude_text": mag, "cap": cap},
            )
