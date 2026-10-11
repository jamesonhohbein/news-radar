"""State media Telegram channels from the public web preview (specs/
state-telegram.md R45 to R50). `https://t.me/s/<handle>` serves 20 posts per
page with no account; `?before=<id>` pages back. A channel whose preview is
off answers 302 to `https://t.me/<handle>` and is retried once a day. This is
the one source that stores text: the posts are public government broadcasts
and the text is what is being studied."""
from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .. import store
from ..statemedia import UA

SOURCE = "telegram-state"
PREVIEW = "https://t.me/s/"
BACK = timedelta(days=7)
RETRY = timedelta(days=1)
HANDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

Fetch = Callable[[str], tuple[int, bytes]]


@dataclass
class Post:
    post_id: int
    posted_at: datetime
    text: str | None
    views: int | None
    forwarded_from: str | None
    links: list[str] = field(default_factory=list)
    media: list[str] = field(default_factory=list)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http_fetch(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with _opener.open(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


# --- Parsing -----------------------------------------------------------------

def count(s: str | None) -> int | None:
    """'34.9K' -> 34900, '1.2M' -> 1200000, '812' -> 812."""
    if not s:
        return None
    s = s.strip().replace(",", "")
    mult = {"K": 1_000, "M": 1_000_000}.get(s[-1:].upper(), 1)
    try:
        return round(float(s[:-1] if mult > 1 else s) * mult)
    except ValueError:
        return None


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _first(pattern: str, s: str) -> str | None:
    m = re.search(pattern, s, re.S)
    return m.group(1) if m else None


_MEDIA = {"photo": "tgme_widget_message_photo_wrap", "video": "tgme_widget_message_video",
          "document": "tgme_widget_message_document", "voice": "tgme_widget_message_voice",
          "poll": "tgme_widget_message_poll", "sticker": "tgme_widget_message_sticker"}


def parse_page(content: str) -> tuple[dict, list[Post]]:
    """Channel info (title, subscribers) and the page's posts, oldest first."""
    info = {"title": _first(r'tgme_channel_info_header_title"><span[^>]*>([^<]+)', content),
            "subscribers": count(_first(r'counter_value">([^<]+)</span>\s*<span class="counter_type">subscribers', content))}
    if info["title"]:
        info["title"] = html.unescape(info["title"]).strip()
    posts = []
    for chunk in content.split('<div class="tgme_widget_message_wrap')[1:]:
        ref = _first(r'data-post="[^"/]+/(\d+)"', chunk)
        when = _first(r'<time datetime="([^"]+)"', chunk)
        if not ref or not when:
            continue
        body = _first(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk)
        links = re.findall(r'href="(https?://[^"]+)"', body or "")
        preview = _first(r'class="tgme_widget_message_link_preview" href="([^"]+)"', chunk)
        if preview and preview not in links:
            links.append(preview)
        fwd = _first(r'tgme_widget_message_forwarded_from_name" href="https://t\.me/([^"/?]+)', chunk) \
            or _first(r'tgme_widget_message_forwarded_from_name"[^>]*>(?:<span[^>]*>)?([^<]+)', chunk)
        posts.append(Post(
            post_id=int(ref),
            posted_at=datetime.fromisoformat(when),
            text=_text(body) if body else None,
            views=count(_first(r'tgme_widget_message_views">([^<]+)<', chunk)),
            forwarded_from=html.unescape(fwd).strip() if fwd else None,
            links=links,
            media=[k for k, cls in _MEDIA.items() if cls in chunk],
        ))
    posts.sort(key=lambda p: p.post_id)
    return info, posts


# --- Channels (R45) ----------------------------------------------------------

def channels(conn) -> list[tuple[str, int]]:
    """Registry Telegram handles worth fetching: valid usernames, no bots."""
    rows = conn.execute("SELECT value, outlet_id FROM state_outlet_id WHERE kind = 'telegram' ORDER BY value").fetchall()
    return [(h, o) for h, o in rows if HANDLE.match(h) and not h.lower().endswith("bot")]


# --- Collection (R46 to R48, R50) -------------------------------------------

def _save_channel(conn, handle, outlet_id, info, preview, now):
    conn.execute("""INSERT INTO tg_channel (handle, outlet_id, title, subscribers, preview, checked_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (handle) DO UPDATE SET outlet_id = EXCLUDED.outlet_id,
                        title = coalesce(EXCLUDED.title, tg_channel.title),
                        subscribers = coalesce(EXCLUDED.subscribers, tg_channel.subscribers),
                        preview = EXCLUDED.preview, checked_at = EXCLUDED.checked_at""",
                 (handle, outlet_id, info.get("title"), info.get("subscribers"), preview, now))


def _save_posts(conn, handle, posts, now) -> int:
    new = 0
    for p in posts:
        r = conn.execute("""INSERT INTO tg_post (handle, post_id, posted_at, text, views, forwarded_from,
                                                 links, media, first_seen, last_seen)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (handle, post_id) DO UPDATE SET text = EXCLUDED.text,
                                views = EXCLUDED.views, links = EXCLUDED.links, media = EXCLUDED.media,
                                last_seen = EXCLUDED.last_seen
                            RETURNING (xmax = 0)""",
                         (handle, p.post_id, p.posted_at, p.text, p.views, p.forwarded_from,
                          p.links, p.media, now, now)).fetchone()
        new += r[0]
    return new


def collect_channel(conn, handle: str, outlet_id: int, fetch: Fetch = http_fetch,
                    now: datetime | None = None, pause: float = 1.0) -> tuple[int, int]:
    """Fetch new posts for one channel. Returns (posts seen, posts new)."""
    now = now or datetime.now(timezone.utc)
    row = conn.execute("SELECT preview, checked_at FROM tg_channel WHERE handle = %s", (handle,)).fetchone()
    if row and not row[0] and now - row[1] < RETRY:
        return 0, 0
    last = conn.execute("SELECT max(post_id) FROM tg_post WHERE handle = %s", (handle,)).fetchone()[0]
    seen = new = 0
    url = PREVIEW + handle
    info: dict = {}
    while url:
        status, body = fetch(url)
        if status in (301, 302):
            _save_channel(conn, handle, outlet_id, {}, False, now)
            return seen, new
        if status != 200:
            raise RuntimeError(f"{url}: HTTP {status}")
        page_info, posts = parse_page(body.decode("utf-8", "replace"))
        info = info or page_info
        _save_channel(conn, handle, outlet_id, info, True, now)
        seen += len(posts)
        new += _save_posts(conn, handle, posts, now)
        if (not posts or posts[0].post_id <= 1 or (last is not None and posts[0].post_id <= last)
                or posts[0].posted_at < now - BACK):
            break
        url = f"{PREVIEW}{handle}?before={posts[0].post_id}"
        time.sleep(pause)
    return seen, new


def run(conn, fetch: Fetch = http_fetch, now: datetime | None = None, pause: float = 1.0) -> list[str]:
    """Every channel, one commit and one fetch_log row per channel. A channel
    that fails is logged and skipped, so one bad page does not stop the rest."""
    now = now or datetime.now(timezone.utc)
    sid = store.source_id(conn, SOURCE)
    errors = []
    for handle, outlet_id in channels(conn):
        try:
            seen, new = collect_channel(conn, handle, outlet_id, fetch, now, pause)
            store.log_fetch(conn, sid, f"{handle}/{now:%Y-%m-%dT%H}", seen, new)
            conn.commit()
        except Exception as e:  # noqa: BLE001  (logged, next channel)
            conn.rollback()
            errors.append(f"{handle}: {e}")
        time.sleep(pause)
    return errors
