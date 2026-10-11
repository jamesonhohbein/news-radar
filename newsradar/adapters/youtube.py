"""State media YouTube channels through an Invidious instance (specs/
state-youtube.md R51 to R56). YouTube's keyless channel feed answers 404, so
the source is Invidious' `/api/v1/channels/<id>/videos` (the 60 latest
uploads) plus `/api/v1/videos/<id>` for each new video's description.
`NEWSRADAR_INVIDIOUS_URL` names the instance; unset, nothing is fetched.
Like Telegram, this stores text: titles and descriptions of public
government uploads, registry channels only."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from .. import store
from ..db import load_env
from ..statemedia import UA
from .telegram import count

SOURCE = "youtube-state"
CHANNEL = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
DUE = timedelta(minutes=55)
RETRY = timedelta(days=7)
# Invidious error messages for a channel YouTube has taken down (R54).
GONE = re.compile(r"terminated|removed|not available|unavailable|does not exist", re.I)

Fetch = Callable[[str], tuple[int, dict]]


class Throttled(Exception):
    """Invidious answered 429 or 403: stop the run (R56)."""


def http_fetch(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def base_url() -> str | None:
    url = load_env().get("NEWSRADAR_INVIDIOUS_URL", "").strip()
    return url.rstrip("/") or None


# --- Channels (R51) ----------------------------------------------------------

def channels(conn) -> list[tuple[str, int]]:
    rows = conn.execute("SELECT value, outlet_id FROM state_outlet_id WHERE kind = 'youtube' ORDER BY value").fetchall()
    return [(c, o) for c, o in rows if CHANNEL.match(c)]


# --- Collection (R52 to R54, R56) --------------------------------------------

def _get(fetch: Fetch, url: str) -> tuple[int, dict]:
    status, body = fetch(url)
    if status in (403, 429):
        raise Throttled(f"{url}: HTTP {status}")
    return status, body


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _save_channel(conn, channel_id, outlet_id, status, reason, now, title=None, subscribers=None):
    conn.execute("""INSERT INTO yt_channel (channel_id, outlet_id, title, subscribers, status, reason, checked_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (channel_id) DO UPDATE SET outlet_id = EXCLUDED.outlet_id,
                        title = coalesce(EXCLUDED.title, yt_channel.title),
                        subscribers = coalesce(EXCLUDED.subscribers, yt_channel.subscribers),
                        status = EXCLUDED.status, reason = EXCLUDED.reason, checked_at = EXCLUDED.checked_at""",
                 (channel_id, outlet_id, title, subscribers, status, reason, now))


def collect_channel(conn, base: str, channel_id: str, outlet_id: int, fetch: Fetch = http_fetch,
                    now: datetime | None = None, pause: float = 1.0) -> tuple[int, int] | None:
    """One channel's latest uploads. Returns (videos seen, videos new), or
    None when the channel is not due."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT status, checked_at FROM yt_channel WHERE channel_id = %s", (channel_id,)).fetchone()
    if row and now - row[1] < (RETRY if row[0] == "gone" else DUE):
        return None
    status, body = _get(fetch, f"{base}/api/v1/channels/{channel_id}/videos")
    if status != 200 or "videos" not in body:
        err = str(body.get("error") or f"HTTP {status}")
        if GONE.search(err):
            _save_channel(conn, channel_id, outlet_id, "gone", err, now)
            return 0, 0
        raise RuntimeError(f"{channel_id}: {err[:200]}")
    videos = [v for v in body["videos"] if v.get("videoId") and not v.get("isUpcoming")]
    known = {r[0] for r in conn.execute("SELECT video_id FROM yt_video WHERE video_id = ANY(%s)",
                                        ([v["videoId"] for v in videos],))}
    title = videos[0].get("author") if videos else None
    subscribers = None
    # The channel row must exist before its videos reference it.
    _save_channel(conn, channel_id, outlet_id, "live", None, now, title)
    new = 0
    for v in videos:
        vid = v["videoId"]
        if vid in known:
            conn.execute("""UPDATE yt_video SET views = coalesce(%s, views), title = %s, last_seen = %s
                            WHERE video_id = %s""", (_int(v.get("viewCount")), v.get("title") or "", now, vid))
            continue
        time.sleep(pause)
        dstatus, d = _get(fetch, f"{base}/api/v1/videos/{vid}")
        if dstatus != 200 or "error" in d:
            d = {}
        subscribers = subscribers or count(d.get("subCountText"))
        # The list's relative time ("12 minutes ago") is precise for a new
        # upload; the detail's `published` is the date at midnight.
        posted = datetime.fromtimestamp(_int(v.get("published")) or now.timestamp(), timezone.utc)
        conn.execute("""INSERT INTO yt_video (video_id, channel_id, published_at, title, description, keywords,
                                              length_s, views, live, first_seen, last_seen)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (vid, channel_id, posted, d.get("title") or v.get("title") or "", d.get("description"),
                      d.get("keywords") or [], _int(v.get("lengthSeconds")),
                      _int(d.get("viewCount", v.get("viewCount"))), bool(v.get("liveNow")), now, now))
        new += 1
    if subscribers:
        _save_channel(conn, channel_id, outlet_id, "live", None, now, title, subscribers)
    return len(videos), new


def run(conn, fetch: Fetch = http_fetch, now: datetime | None = None, pause: float = 1.0,
        base: str | None = None) -> list[str]:
    """Every due channel, one commit and one fetch_log row per channel. A
    channel that fails is logged and skipped; a 429 or 403 ends the run.
    Without an Invidious URL nothing is fetched."""
    base = base or base_url()
    if not base:
        return []
    now = now or datetime.now(timezone.utc)
    sid = store.source_id(conn, SOURCE)
    errors = []
    for channel_id, outlet_id in channels(conn):
        try:
            got = collect_channel(conn, base, channel_id, outlet_id, fetch, now, pause)
            if got is None:
                continue
            store.log_fetch(conn, sid, f"{channel_id}/{now:%Y-%m-%dT%H}", *got)
            conn.commit()
        except Throttled as e:
            conn.rollback()
            store.log_fetch(conn, sid, f"throttled/{now:%Y-%m-%dT%H}", 0, 0)
            conn.commit()
            errors.append(str(e))
            break
        except Exception as e:  # noqa: BLE001  (logged, next channel)
            conn.rollback()
            errors.append(f"{channel_id}: {e}")
        time.sleep(pause)
    return errors
