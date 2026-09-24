"""GDELT 2.0 adapter: Events, Mentions (R18) and GKG (R22).

Every 15 minutes GDELT publishes export.CSV.zip: tab-separated, no header,
61 columns in the order of the v2 codebook, pinned below as COLUMNS. Only
events with a resolvable ActionGeo point are kept; the rest are counted and
dropped, because a map cannot show them and the attention series is spatial.

Mentions (`mentions.CSV.zip`, 16 columns) has one row per article that
mentions an event, including events first seen days earlier. It is the only
place later coverage appears; Events counts stop after the first window.

GKG (`gkg.csv.zip`, 27 columns) has one row per article with every theme
and every place it names. Rows run to hundreds of KB (quotes, entities,
counts), so only id, date, site, URL, tone, V1 themes and V1 locations are
read.

Use https. The http host answers 301 and curl-style clients that do not
follow redirects see an empty body, which looks like GDELT being down.
"""
from __future__ import annotations

import csv
import io
import urllib.request
import zipfile
from datetime import date, datetime, timezone
from dataclasses import dataclass
from typing import Iterable, Iterator

from . import Event

KIND = "gdelt"
BASE = "https://data.gdeltproject.org/gdeltv2/"

COLUMNS = (
    "GlobalEventID", "Day", "MonthYear", "Year", "FractionDate",
    "Actor1Code", "Actor1Name", "Actor1CountryCode", "Actor1KnownGroupCode", "Actor1EthnicCode",
    "Actor1Religion1Code", "Actor1Religion2Code", "Actor1Type1Code", "Actor1Type2Code", "Actor1Type3Code",
    "Actor2Code", "Actor2Name", "Actor2CountryCode", "Actor2KnownGroupCode", "Actor2EthnicCode",
    "Actor2Religion1Code", "Actor2Religion2Code", "Actor2Type1Code", "Actor2Type2Code", "Actor2Type3Code",
    "IsRootEvent", "EventCode", "EventBaseCode", "EventRootCode", "QuadClass",
    "GoldsteinScale", "NumMentions", "NumSources", "NumArticles", "AvgTone",
    "Actor1Geo_Type", "Actor1Geo_Fullname", "Actor1Geo_CountryCode", "Actor1Geo_ADM1Code",
    "Actor1Geo_ADM2Code", "Actor1Geo_Lat", "Actor1Geo_Long", "Actor1Geo_FeatureID",
    "Actor2Geo_Type", "Actor2Geo_Fullname", "Actor2Geo_CountryCode", "Actor2Geo_ADM1Code",
    "Actor2Geo_ADM2Code", "Actor2Geo_Lat", "Actor2Geo_Long", "Actor2Geo_FeatureID",
    "ActionGeo_Type", "ActionGeo_Fullname", "ActionGeo_CountryCode", "ActionGeo_ADM1Code",
    "ActionGeo_ADM2Code", "ActionGeo_Lat", "ActionGeo_Long", "ActionGeo_FeatureID",
    "DATEADDED", "SOURCEURL",
)
assert len(COLUMNS) == 61
IDX = {name: i for i, name in enumerate(COLUMNS)}


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "news-radar/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


EXPORT = ".export.CSV.zip"
MENTIONS = ".mentions.CSV.zip"
GKG = ".gkg.csv.zip"


def _files(listing: bytes, suffix: str = EXPORT) -> list[str]:
    """`size md5 url` per line; keep one file type, as bare file names."""
    out = []
    for line in listing.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].endswith(suffix):
            out.append(parts[2].rsplit("/", 1)[1])
    return out


def file_stamp(file: str) -> datetime:
    return datetime.strptime(file[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def latest(suffix: str = EXPORT) -> str:
    return _files(_get(BASE + "lastupdate.txt"), suffix)[0]


def list_files(since: datetime, suffix: str = EXPORT) -> list[str]:
    files = _files(_get(BASE + "masterfilelist.txt", timeout=180), suffix)
    return [f for f in files if file_stamp(f) >= since]


def fetch(file: str) -> bytes:
    return _get(BASE + file)


def _opt(s: str) -> str | None:
    return s or None


def _f(s: str) -> float | None:
    return float(s) if s else None


def _i(s: str) -> int | None:
    return int(s) if s else None


def parse(file: str, blob: bytes) -> Iterator[tuple[Event | None, bool]]:
    """Yield (event, kept). kept=False rows are counted, not stored."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        text = z.read(name).decode("utf-8", "replace")
    yield from parse_rows(csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE))


def parse_rows(rows: Iterable[list[str]]) -> Iterator[tuple[Event | None, bool]]:
    for row in rows:
        if len(row) != 61:
            yield None, False
            continue
        g = lambda k: row[IDX[k]]  # noqa: E731
        if g("ActionGeo_Type") in ("", "0") or not g("ActionGeo_Lat") or not g("ActionGeo_Long") or not g("ActionGeo_CountryCode"):
            yield None, False
            continue
        try:
            occurred = datetime.strptime(g("Day"), "%Y%m%d").date()
        except ValueError:
            occurred = None
        yield Event(
            external_id=g("GlobalEventID"),
            occurred_on=occurred,
            added_at=datetime.strptime(g("DATEADDED"), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc),
            cameo_code=_opt(g("EventCode")),
            cameo_root=_opt(g("EventRootCode")),
            quad_class=_i(g("QuadClass")),
            goldstein=_f(g("GoldsteinScale")),
            tone=_f(g("AvgTone")),
            actor1_name=_opt(g("Actor1Name")),
            actor1_country=_opt(g("Actor1CountryCode")),
            actor2_name=_opt(g("Actor2Name")),
            actor2_country=_opt(g("Actor2CountryCode")),
            geo_type=_i(g("ActionGeo_Type")),
            geo_name=_opt(g("ActionGeo_Fullname")),
            country=g("ActionGeo_CountryCode"),
            adm1=_opt(g("ActionGeo_ADM1Code")),
            lat=float(g("ActionGeo_Lat")),
            lon=float(g("ActionGeo_Long")),
            num_mentions=int(g("NumMentions") or 0),
            num_sources=int(g("NumSources") or 0),
            num_articles=int(g("NumArticles") or 0),
            url=_opt(g("SOURCEURL")),
        ), True


MENTION_COLUMNS = 16


def parse_mentions(blob: bytes) -> Iterator[tuple[str, datetime, str] | None]:
    """Yield (GlobalEventID, MentionTimeDate, MentionSourceName), or None for a
    malformed row so the caller can count it."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    yield from parse_mention_rows(csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE))


def parse_mention_rows(rows: Iterable[list[str]]) -> Iterator[tuple[str, datetime, str] | None]:
    for row in rows:
        if len(row) < MENTION_COLUMNS - 2 or not row[0] or not row[4]:
            yield None
            continue
        try:
            t = datetime.strptime(row[2], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            yield None
            continue
        yield row[0], t, row[4]


@dataclass(frozen=True)
class GkgLocation:
    loc_type: int      # 1 country, 2 US state, 3 US city, 4 world city, 5 world ADM1
    country: str       # FIPS, as Events codes it
    adm1: str | None   # None for type 1, and when GDELT only knows the country
    lat: float
    lon: float


@dataclass(frozen=True)
class GkgArticle:
    gkg_id: str
    added_at: datetime
    site: str | None
    url: str | None
    tone: float | None
    themes: tuple[str, ...]
    locations: tuple[GkgLocation, ...]


def parse_gkg(blob: bytes) -> Iterator[GkgArticle | None]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    yield from parse_gkg_lines(text.splitlines())


def parse_gkg_lines(lines: Iterable[str]) -> Iterator[GkgArticle | None]:
    """Split on tabs directly: GKG fields carry stray quotes that trip csv."""
    for line in lines:
        r = line.split("\t")
        if len(r) < 16 or not r[0]:
            yield None
            continue
        try:
            added = datetime.strptime(r[1], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            yield None
            continue
        themes = tuple(dict.fromkeys(t for t in r[7].split(";") if t))
        locs = {}
        for part in r[9].split(";"):
            f = part.split("#")
            if len(f) < 6 or not f[2] or not f[4] or not f[5]:
                continue
            try:
                typ, lat, lon = int(f[0]), float(f[4]), float(f[5])
            except ValueError:
                continue
            adm1 = f[3] if typ != 1 and f[3] and f[3] != f[2] else None
            locs.setdefault((typ, f[2], adm1, lat, lon), GkgLocation(typ, f[2], adm1, lat, lon))
        try:
            tone = float(r[15].split(",")[0]) if r[15] else None
        except ValueError:
            tone = None
        yield GkgArticle(r[0], added, r[3] or None, r[4] or None, tone, themes, tuple(locs.values()))
