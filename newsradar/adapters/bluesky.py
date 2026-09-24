"""Bluesky public posts (R31), from Jetstream. No key. Posts carry no
location and users keep the rights to them, so nothing of a post is stored:
posts are matched against a GeoNames gazetteer and only per-place counts are
kept, plus a short-lived pointer (the at:// URI) to a few posts per place for
drill-down, fetched live when displayed and dropped when the post is deleted.

Matching is case-sensitive: a name must appear as written ("Nice", not
"nice"), with word boundaries where the script has case. Where a name
belongs to several places, a country wins over a city, and a city over a
first-level division; among cities, the most populous."""
from __future__ import annotations

import json
import unicodedata
from typing import Iterable, Iterator

import ahocorasick

KIND = "bluesky"
SOURCE = "bluesky-posts"
FEED = "wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post"
KIND_RANK = {"country": 0, "city": 1, "adm1": 2}


def _script(ch: str) -> str:
    n = unicodedata.name(ch, "")
    if n.startswith("CJK UNIFIED") or n.startswith("CJK COMPATIBILITY IDEOGRAPH"):
        return "han"
    if n.startswith(("HIRAGANA", "KATAKANA")) or n.startswith("THAI") or n.startswith(("LAO ", "KHMER", "MYANMAR")):
        return "unspaced"   # scripts written without spaces between words
    return "spaced"


def usable(name: str, primary: bool = True) -> bool:
    """A name worth matching. No digits. Cased scripts: starts uppercase
    (drops lowercase transliterations); an all-caps alternate of 4 letters or
    fewer is an airport or station code ("BBC", "USA") and is dropped. Han
    names need 2 characters; kana, Thai and similar need 3, because a short
    transliteration (ライ, "rai") sits inside ordinary words."""
    name = name.strip()
    if not name or any(ch.isdigit() for ch in name):
        return False
    c = name[0]
    if c.isupper():
        if not primary and name.isupper() and len(name) <= 4:
            return False
        return len(name) >= 3
    if c.islower() or not unicodedata.category(c).startswith("L"):
        return False
    s = _script(c)
    return len(name) >= (2 if s == "han" else 3)


def _bounded(name: str) -> bool:
    """Whether a match must sit on word boundaries (any script with spaces)."""
    return _script(name[0]) == "spaced"


def build(names: Iterable[tuple]):
    """names: (place_id, name, kind, population[, is_primary]). One place per name."""
    best: dict[str, tuple[int, int, int]] = {}
    for pid, name, kind, pop, *rest in names:
        if not usable(name, rest[0] if rest else True):
            continue
        key = (KIND_RANK[kind], -(pop or 0), pid)
        if name not in best or key < best[name]:
            best[name] = key
    a = ahocorasick.Automaton()
    for name, (_, _, pid) in best.items():
        a.add_word(name, (pid, name))
    a.make_automaton()
    return a


def match(automaton, text: str) -> set[int]:
    """Place ids named in text. For cased scripts the match must sit on word
    boundaries; overlapping shorter names inside a longer match are dropped."""
    hits = []
    for end, (pid, name) in automaton.iter(text):
        start = end - len(name) + 1
        if _bounded(name):
            before = text[start - 1] if start > 0 else " "
            after = text[end + 1] if end + 1 < len(text) else " "
            if before.isalnum() or after.isalnum():
                continue
        hits.append((start, end, pid))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    out, last_end = set(), -1
    for s, e, pid in hits:
        if s > last_end:
            out.add(pid)
            last_end = e
    return out


def parse(msg: str | bytes) -> tuple[str, int, str | None, str | None] | None:
    """Jetstream message -> (op, time_us, uri, text). None if not a post commit."""
    d = json.loads(msg)
    c = d.get("commit") or {}
    if d.get("kind") != "commit" or c.get("collection") != "app.bsky.feed.post":
        return None
    uri = f"at://{d['did']}/app.bsky.feed.post/{c.get('rkey')}"
    if c.get("operation") == "create":
        return "create", d["time_us"], uri, (c.get("record") or {}).get("text") or ""
    if c.get("operation") == "delete":
        return "delete", d["time_us"], uri, None
    return None


def gazetteer_rows(cities_txt: Iterable[str], admin1_txt: Iterable[str], countries: Iterable[tuple[str, str, str]]
                   ) -> Iterator[tuple]:
    """GeoNames files -> (id, name, kind, iso2, admin1, lat, lon, population, names[]).
    countries: (iso2, name, geonameid). ADM1 points are filled in SQL from
    their cities, countries from country_shape."""
    for line in cities_txt:
        f = line.rstrip("\n").split("\t")
        if len(f) < 15:
            continue
        names = {f[1], f[2], *[n for n in f[3].split(",") if n]}
        yield (int(f[0]), f[1], "city", f[8], f[10], float(f[4]), float(f[5]), int(f[14] or 0), sorted(names))
    for line in admin1_txt:
        f = line.rstrip("\n").split("\t")
        if len(f) < 4 or "." not in f[0]:
            continue
        iso, code = f[0].split(".", 1)
        yield (int(f[3]), f[1], "adm1", iso, code, None, None, 0, sorted({f[1], f[2]}))
    for iso, name, gid in countries:
        if gid:
            yield (int(gid), name, "country", iso, None, None, None, 0, [name])
