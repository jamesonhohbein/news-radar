"""Wikipedia edit activity (R30), from Wikimedia EventStreams. No key.
Content is CC BY-SA, but nothing of it is stored: only which page was edited,
when, a truncated hash of the editor, and whether it was new or a bot.

`recentchange` carries every Wikimedia wiki with no server-side filter
(Wikidata and Commons are most of it), so the consumer keeps namespace 0 on
*.wikipedia.org only. Page creations arrive here as type "new", so the
separate page-create stream is not needed. Coordinates come from each
wiki's GeoData API for pages that get busy (see HOT_EDITORS), cached in
`wiki_page` and rechecked after 7 days."""
from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterable, Iterator

KIND = "wikipedia"
SOURCE = "wikipedia-edits"
FEED = "https://stream.wikimedia.org/v2/stream/recentchange"
UA = "news-radar/0.1 (github.com/jamesonhohbein/news-radar)"
HOT_EDITORS = 3        # human editors in an hour that make a page worth geolocating
RECHECK_DAYS = 7


def sse(lines: Iterable[str]) -> Iterator[tuple[str | None, dict]]:
    """Server-sent events -> (id, data). Comments and non-message events skipped."""
    eid, data = None, []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                try:
                    yield eid, json.loads("\n".join(data))
                except json.JSONDecodeError:
                    pass
            eid, data = None, []
        elif line.startswith("id:"):
            eid = line[3:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


def keep(d: dict) -> bool:
    return (d.get("namespace") == 0 and d.get("type") in ("edit", "new")
            and str(d.get("server_name", "")).endswith(".wikipedia.org"))


def row(d: dict) -> tuple:
    """(wiki, server, title, at, user_hash, bot, created, rev)."""
    user = (d.get("user") or "").encode()
    return (d["wiki"], d["server_name"], d["title"],
            datetime.fromtimestamp(d["timestamp"], tz=timezone.utc),
            hashlib.sha256(user).hexdigest()[:12], bool(d.get("bot")), d["type"] == "new",
            (d.get("revision") or {}).get("new"))


def open_stream(last_id: str | None):
    headers = {"User-Agent": UA, "Accept": "text/event-stream"}
    if last_id:
        headers["Last-Event-ID"] = last_id
    return urllib.request.urlopen(urllib.request.Request(FEED, headers=headers), timeout=90)


def geodata(server: str, titles: list[str]) -> dict[str, tuple[float, float] | None]:
    """Primary coordinates for up to 50 titles on one wiki; None where absent."""
    q = urllib.parse.urlencode({"action": "query", "prop": "coordinates", "coprimary": "primary",
                                "titles": "|".join(titles), "format": "json", "formatversion": 2, "redirects": 1})
    req = urllib.request.Request(f"https://{server}/w/api.php?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read())
    back = {t: t for t in titles}
    for n in (d.get("query", {}).get("normalized", []) + d.get("query", {}).get("redirects", [])):
        back[n["to"]] = back.get(n["from"], n["from"])
    out: dict[str, tuple[float, float] | None] = {t: None for t in titles}
    for p in d.get("query", {}).get("pages", []):
        c = (p.get("coordinates") or [None])[0]
        orig = back.get(p.get("title"), p.get("title"))
        if c and orig in out:
            out[orig] = (c["lat"], c["lon"])
    return out
