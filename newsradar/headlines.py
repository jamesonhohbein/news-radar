"""Title extraction and story selection (R16). Pure functions here; the
fetch loop and the CLI are in headlines.py at the repo root."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

MIN_SOURCES = 2
MAX_ATTEMPTS = 3
RETRY_AFTER_HOURS = 6
WINDOW_HOURS = 24

# URLs at the threshold in the window that have no story row, or a failed
# one that is old enough and under the attempt cap. A URL qualifies on its
# first-window sources, or on distinct outlets mentioning its event within
# the window (R20), so a story that grows after its first 15 minutes gets a
# headline too.
SELECT_SQL = """
SELECT e.url
FROM (
    SELECT url, max(ns) AS ns FROM (
        SELECT url, num_sources AS ns
        FROM event
        WHERE added_at > now() - make_interval(hours => %(window)s) AND url IS NOT NULL
        UNION ALL
        SELECT e.url, count(DISTINCT m.source_name)
        FROM mention m JOIN event e ON e.id = m.event_id
        WHERE m.mentioned_at > now() - make_interval(hours => %(window)s) AND e.url IS NOT NULL
        GROUP BY e.url
    ) u
    GROUP BY url
) e
LEFT JOIN story s ON s.url = e.url
WHERE e.ns >= %(min_sources)s
  AND (s.url IS NULL
       OR (s.status = 'fail' AND s.attempts < %(max_attempts)s
           AND s.fetched_at < now() - make_interval(hours => %(retry_after)s)))
ORDER BY e.ns DESC
"""

UPSERT_SQL = """
INSERT INTO story (url, title, site, status, attempts, fetched_at)
VALUES (%(url)s, %(title)s, %(site)s, %(status)s, 1, now())
ON CONFLICT (url) DO UPDATE
SET title = EXCLUDED.title, site = EXCLUDED.site, status = EXCLUDED.status,
    attempts = story.attempts + 1, fetched_at = now()
"""


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.og: str | None = None
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.title:
            self._in_title = True
        elif tag == "meta" and self.og is None:
            a = dict(attrs)
            if a.get("property") == "og:title" and a.get("content"):
                self.og = a["content"]
        elif tag == "body":
            self.done = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)


def extract_title(page: str) -> str | None:
    """og:title wins when present: it is the editorial headline, while
    <title> usually carries " | Site Name" and sometimes only the site."""
    p = _TitleParser()
    try:
        p.feed(page)
    except Exception:  # noqa: BLE001  malformed HTML is the norm, take what parsed
        pass
    for cand in (p.og, "".join(p.title)):
        if cand:
            t = re.sub(r"\s+", " ", html.unescape(cand)).strip()
            if t:
                return t[:300]
    return None


def site_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host
