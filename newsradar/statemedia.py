"""State media registry (specs/state-media.md R39 to R43).

Outlets and their typology come from State Media Monitor (MJRC, CC BY 4.0),
which publishes no bulk file: three regional pages embed a `const DATA`
array, and newer outlet profiles carry a Quick Facts card. Hostnames and
channel handles come from Wikidata (CC0), plus a committed manual CSV.

A label, not a filter: every typology is stored, including the editorially
independent public broadcasters. The lens picks typologies in its WHERE."""
from __future__ import annotations

import csv
import html
import json
import re
import time
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

SMM = "https://statemediamonitor.com/wp-json/wp/v2/posts"
WIKIDATA = "https://www.wikidata.org/w/api.php"
UA = "news-radar/0.1 (https://github.com/jamesonhohbein/news-radar)"
MANUAL = Path(__file__).resolve().parent.parent / "data" / "state_media_ids.csv"

YEARS = 5  # trajectory covers 2022..2026
# Wikidata property per identifier kind (R40).
PROPS = {"P856": "host", "P3789": "telegram", "P2397": "youtube", "P2002": "x"}

Fetch = Callable[[str], bytes]

# SMM country names that differ from GeoNames' (country_code.name).
SMM_ISO2 = {"netherlands": "NL", "palestinian territories": "PS", "the maldives": "MV", "uae": "AE"}


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


@dataclass
class Outlet:
    country: str
    name: str
    typology: str
    trajectory: list[str]
    smm_url: str
    region: str | None = None
    assets: str | None = None


# --- State Media Monitor (R39) ------------------------------------------------

def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_data(content: str, url: str) -> list[Outlet]:
    """Outlets from a regional page's embedded `const DATA = [...]` array."""
    m = re.search(r"const DATA\s*=\s*(\[.*?\]);\s*\n", content, re.S)
    if not m:
        return []
    out = []
    for sr in json.loads(m.group(1)):
        for co in sr.get("countries", []):
            for o in co.get("outlets", []):
                t = [x or "-" for x in o.get("t", [])]
                if not t or t[-1] == "-":
                    continue
                out.append(Outlet(co["country"], o["name"], t[-1], t, url,
                                  region=sr.get("sr") or None, assets=o.get("assets") or None))
    return out


def parse_profile(content: str, title: str, url: str) -> Outlet | None:
    """An outlet from a profile page's Quick Facts card, or None if the post
    is not one (regional overviews, commentary, removed outlets)."""
    m = re.search(r">([^<>]+?)\s*&middot;\s*Quick Facts</span>.*?<div[^>]*>\s*([A-Za-z]{2,5})\s*</div>", content, re.S)
    if not m:
        return None
    # Subnational broadcasters read "Spain · Catalonia · Quick Facts".
    country, _, sub = html.unescape(m.group(1)).strip().partition(" \u00b7 ")
    code = m.group(2)
    traj = ["-"] * YEARS
    t = re.search(r"Typology trajectory.*?2022.{0,40}2026</span>(.*?)</div>\s*</div>\s*</div>", content, re.S)
    if t:
        for year, c in re.findall(r">(20\d\d)</div>\s*<div[^>]*>\s*([A-Za-z-]{1,5})\s*</div>", t.group(1)):
            i = int(year) - 2022
            if 0 <= i < YEARS:
                traj[i] = c
    traj[-1] = code
    return Outlet(country, _text(title).rstrip("*").strip(), code, traj, url, region=sub or None)


def name_keys(name: str) -> set[str]:
    """Normalised name variants, so `Xinhua (New China News Agency)` and
    `Xinhua`, or `Philippines News Agency (PNA)` and `Philippine News Agency
    (PNA)`, are recognised as one outlet."""
    return {re.sub(r"[^a-z0-9]", "", v.casefold()) for v in name_variants(name.rstrip("*").strip())} - {""}


def outlets_from_posts(posts: Iterable[dict]) -> list[Outlet]:
    """DATA rows win over a profile row for the same outlet: same country and
    a shared name variant (R39)."""
    data, profiles = [], []
    for p in posts:
        c, url = p["content"]["rendered"], p["link"]
        data += parse_data(c, url)
        o = parse_profile(c, p["title"]["rendered"], url)
        if o:
            profiles.append(o)
    out, seen = [], {}
    for o in data + profiles:
        keys = seen.setdefault(o.country, set())
        k = name_keys(o.name)
        if k & keys:
            continue
        keys |= k
        out.append(o)
    return out


def fetch_posts(fetch: Fetch = http_get) -> list[dict]:
    posts, page = [], 1
    while True:
        batch = json.loads(fetch(f"{SMM}?per_page=100&page={page}&_fields=link,title,content"))
        posts += batch
        if len(batch) < 100:
            return posts
        page += 1


def upsert_outlets(conn, outlets: list[Outlet], today: date) -> int:
    """Insert new outlets, refresh seen ones; an outlet gone from SMM keeps its
    row and its old last_seen. Returns how many rows were new."""
    new = 0
    for o in outlets:
        r = conn.execute("""
            INSERT INTO state_outlet (country, name, region, typology, trajectory, assets, smm_url, first_seen, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (country, name) DO UPDATE SET
                region = EXCLUDED.region, typology = EXCLUDED.typology, trajectory = EXCLUDED.trajectory,
                assets = EXCLUDED.assets, smm_url = EXCLUDED.smm_url, last_seen = EXCLUDED.last_seen
            RETURNING (xmax = 0)""",
            (o.country, o.name, o.region, o.typology, o.trajectory, o.assets, o.smm_url, today, today)).fetchone()
        new += bool(r[0])
    return new


# --- Wikidata (R40) -----------------------------------------------------------

def name_variants(name: str) -> list[str]:
    """`Xinhua (New China News Agency)` -> the full name, the part before the
    parenthesis and the part inside it."""
    out = [name]
    m = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", name)
    if m:
        out += [m.group(1), m.group(2)]
    return [v for i, v in enumerate(out) if v and v not in out[:i]]


def host_of(url: str) -> str | None:
    h = urllib.parse.urlsplit(url.strip()).hostname
    if not h:
        return None
    h = h.lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _claims(ent: dict, prop: str) -> list:
    out = []
    for c in ent.get("claims", {}).get(prop, []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if v is not None and c.get("rank") != "deprecated":
            out.append(v["id"] if isinstance(v, dict) and "id" in v else v)
    return out


def _names(ent: dict) -> set[str]:
    out = {v["value"].casefold() for v in ent.get("labels", {}).values()}
    for vs in ent.get("aliases", {}).values():
        out |= {v["value"].casefold() for v in vs}
    return out


class Wikidata:
    """wbsearchentities + wbgetentities, cached per run. `fetch` is injectable
    so tests run on fixtures."""

    def __init__(self, fetch: Fetch = http_get, pause: float = 0.1):
        self.fetch, self.pause, self._ents = fetch, pause, {}

    def _get(self, params: dict) -> dict:
        url = WIKIDATA + "?" + urllib.parse.urlencode({**params, "format": "json"})
        # Hundreds of calls per run; a DNS blip or a 429 must not lose the run.
        for attempt in range(4):
            time.sleep(self.pause if attempt == 0 else 5 * 2 ** attempt)
            try:
                return json.loads(self.fetch(url))
            except (urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    raise
        raise AssertionError("unreachable")

    def search(self, text: str) -> list[str]:
        r = self._get({"action": "wbsearchentities", "search": text, "language": "en", "type": "item", "limit": 15})
        return [s["id"] for s in r.get("search", [])]

    def entities(self, qids: Iterable[str]) -> dict[str, dict]:
        want = [q for q in dict.fromkeys(qids) if q not in self._ents]
        for i in range(0, len(want), 50):
            r = self._get({"action": "wbgetentities", "ids": "|".join(want[i:i + 50]),
                           "props": "labels|aliases|claims", "languages": "en"})
            self._ents.update(r.get("entities", {}))
        return {q: self._ents[q] for q in qids if q in self._ents}


def resolve(wd: Wikidata, o: Outlet, iso2: str | None = None) -> tuple[str | None, list[tuple[str, str]]]:
    """The outlet's single in-country Wikidata item and its identifiers, or
    (None, []) when zero or several items qualify (R40: a wrong match is
    worse than a missing one).

    In-country means a P17 whose ISO code (P297) is `iso2`, or whose label or
    alias is SMM's country name: "China" is not an alias of Q148. Several
    in-country items (IRIB and IRIB TV1) narrow to those whose label or alias
    is exactly one of the outlet's name variants."""
    variants = name_variants(o.name)
    cands = []
    for v in variants:
        cands += wd.search(v)
    ents = wd.entities(cands)
    countries = wd.entities({c for e in ents.values() for c in _claims(e, "P17")})

    def in_country(c: str) -> bool:
        ent = countries.get(c, {})
        return (iso2 is not None and iso2.upper() in _claims(ent, "P297")) or o.country.casefold() in _names(ent)

    hits = [q for q, e in ents.items() if any(in_country(c) for c in _claims(e, "P17"))]
    if len(hits) > 1:
        want = {v.casefold() for v in variants}
        hits = [q for q in hits if _names(ents[q]) & want]
    if len(hits) != 1:
        return None, []
    qid = hits[0]
    ents = [ents[qid]] + list(wd.entities(_claims(ents[qid], "P355")).values())
    ids = []
    for e in ents:
        for prop, kind in PROPS.items():
            for v in _claims(e, prop):
                v = host_of(v) if kind == "host" else str(v).strip().lstrip("@")
                if v:
                    ids.append((kind, v.lower() if kind != "youtube" else v))
    return qid, list(dict.fromkeys(ids))


def store_wikidata(conn, matches: dict[int, tuple[str, list[tuple[str, str]]]]) -> int:
    """Replace every wikidata-origin identifier; manual rows win any clash and
    are never touched (R41). Returns rows stored."""
    conn.execute("DELETE FROM state_outlet_id WHERE origin = 'wikidata'")
    n = 0
    for outlet_id, (qid, ids) in matches.items():
        for kind, value in ids:
            r = conn.execute("""INSERT INTO state_outlet_id (outlet_id, kind, value, origin, qid)
                                VALUES (%s, %s, %s, 'wikidata', %s) ON CONFLICT (kind, value) DO NOTHING""",
                             (outlet_id, kind, value, qid))
            n += r.rowcount
    return n


def iso2_map(conn) -> dict[str, str]:
    """SMM country name (casefolded) to ISO alpha-2, from country_code."""
    m = {n.casefold(): i for i, n in conn.execute("SELECT iso2, name FROM country_code")}
    return {**m, **SMM_ISO2}


# --- Manual identifiers (R41) -------------------------------------------------

def load_manual(conn, path: Path = MANUAL) -> int:
    """Rows are country,name,kind,value. A manual row displaces a wikidata row
    for the same identifier. An outlet name not in the registry is an error,
    so a typo cannot silently drop a row."""
    if not path.exists():
        return 0
    n = 0
    with path.open() as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("#")))
    for row in rows:
        r = conn.execute("SELECT id FROM state_outlet WHERE country = %s AND name = %s",
                         (row["country"], row["name"])).fetchone()
        if not r:
            raise ValueError(f"{path.name}: no outlet {row['country']!r} / {row['name']!r}")
        value = host_of("//" + row["value"]) if row["kind"] == "host" else row["value"].strip()
        conn.execute("""INSERT INTO state_outlet_id (outlet_id, kind, value, origin)
                        VALUES (%s, %s, %s, 'manual')
                        ON CONFLICT (kind, value) DO UPDATE SET outlet_id = EXCLUDED.outlet_id,
                            origin = 'manual', qid = NULL""", (r[0], row["kind"], value))
        n += 1
    return n


# --- Coverage report (R43) ----------------------------------------------------

def report(conn) -> dict[str, list[tuple]]:
    no_host = conn.execute("""
        SELECT o.typology, o.country, o.name FROM state_outlet o
        WHERE NOT EXISTS (SELECT 1 FROM state_outlet_id i WHERE i.outlet_id = o.id AND i.kind = 'host')
        ORDER BY 1, 2, 3""").fetchall()
    silent = conn.execute("""
        SELECT o.typology, o.country, o.name, i.value FROM state_outlet_id i JOIN state_outlet o ON o.id = i.outlet_id
        WHERE i.kind = 'host' AND NOT EXISTS (
            SELECT 1 FROM state_media_article a WHERE a.host = i.value AND a.added_at > now() - interval '7 days')
        ORDER BY 1, 2, 3, 4""").fetchall()
    # Collected channels (adapters/telegram.py R45) that yield nothing (R48).
    telegram = conn.execute("""
        SELECT o.typology, o.country, o.name, i.value,
               CASE WHEN c.preview IS FALSE THEN 'no preview' ELSE 'no post in 7 days' END
        FROM state_outlet_id i JOIN state_outlet o ON o.id = i.outlet_id
        LEFT JOIN tg_channel c ON c.handle = i.value
        WHERE i.kind = 'telegram' AND i.value ~ '^[A-Za-z][A-Za-z0-9_]{3,31}$' AND lower(i.value) NOT LIKE '%bot'
          AND (c.preview IS FALSE OR NOT EXISTS (
              SELECT 1 FROM tg_post p WHERE p.handle = i.value AND p.posted_at > now() - interval '7 days'))
        ORDER BY 5, 1, 2, 3, 4""").fetchall()
    return {"no_host": no_host, "silent_host": silent, "telegram": telegram}
