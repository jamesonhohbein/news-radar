"""T11, T12 (R16): title extraction and story selection."""
from datetime import datetime, timedelta, timezone

from newsradar.headlines import MAX_ATTEMPTS, MIN_SOURCES, RETRY_AFTER_HOURS, SELECT_SQL, WINDOW_HOURS, extract_title, site_of
from tests.dbcase import DBCase

PARAMS = {"window": WINDOW_HOURS, "min_sources": MIN_SOURCES, "max_attempts": MAX_ATTEMPTS, "retry_after": RETRY_AFTER_HOURS}


class T11Extract(DBCase.__mro__[1]):
    def test_og_title_wins_over_title(self):
        page = '<html><head><title>Site Name</title><meta property="og:title" content="Quake hits  Noto &amp; Sado"></head><body>'
        self.assertEqual(extract_title(page), "Quake hits Noto & Sado")

    def test_title_fallback_and_whitespace(self):
        page = "<html><head><title>\n  Strong earthquake\n strikes   Japan | Reuters </title></head>"
        self.assertEqual(extract_title(page), "Strong earthquake strikes Japan | Reuters")

    def test_entities_and_no_title(self):
        self.assertEqual(extract_title("<title>Harry &amp; Meghan &#8216;need protection&#8217;</title>"), "Harry & Meghan ‘need protection’")
        self.assertIsNone(extract_title("<html><body><p>no head</p></body></html>"))
        self.assertIsNone(extract_title("<title>   </title>"))

    def test_site(self):
        self.assertEqual(site_of("https://www.example.co.uk/news/1"), "example.co.uk")


class T12Select(DBCase):
    def _ev(self, url, ns, hours_ago=1):
        self.conn.execute("""INSERT INTO event (source_id, external_id, added_at, country, geom, num_mentions, num_sources, num_articles, url)
                             VALUES (1, %s, now() - make_interval(hours => %s), 'US', ST_SetSRID(ST_MakePoint(0,0),4326), 1, %s, 1, %s)""",
                          (f"{url}-{ns}-{hours_ago}", hours_ago, ns, url))

    def _story(self, url, status, attempts, hours_ago):
        self.conn.execute("INSERT INTO story (url, status, attempts, fetched_at) VALUES (%s, %s, %s, now() - make_interval(hours => %s))",
                          (url, status, attempts, hours_ago))

    def _selected(self):
        return [r[0] for r in self.conn.execute(SELECT_SQL, PARAMS).fetchall()]

    def test_threshold_window_and_retry_rules(self):
        self.conn.execute("TRUNCATE story")
        self._ev("https://a/1", 5)                 # new, above threshold: picked
        self._ev("https://a/2", 1)                 # below threshold: not picked
        self._ev("https://a/3", 5, hours_ago=30)   # outside window: not picked
        self._ev("https://a/4", 5); self._story("https://a/4", "ok", 1, 1)        # fetched: not re-picked
        self._ev("https://a/5", 5); self._story("https://a/5", "fail", 1, 1)      # failed 1 h ago: too soon
        self._ev("https://a/6", 5); self._story("https://a/6", "fail", 1, 7)      # failed 7 h ago: retry
        self._ev("https://a/7", 5); self._story("https://a/7", "fail", 3, 48)     # attempts exhausted
        self._ev("https://a/8", 1); self._ev("https://a/8", 3)                    # max over events counts
        # Grew after its first window: an old event, 1 first-window source,
        # 3 outlets mentioning it in the last hour. Picked. One outlet: not.
        for url, outlets in (("https://a/9", 3), ("https://a/10", 1)):
            eid = self.conn.execute("""INSERT INTO event (source_id, external_id, added_at, country, geom, num_mentions, num_sources, num_articles, url)
                                       VALUES (1, %s, now() - interval '30 hours', 'US', ST_SetSRID(ST_MakePoint(0,0),4326), 1, 1, 1, %s)
                                       RETURNING id""", (url, url)).fetchone()[0]
            for i in range(outlets):
                self.conn.execute("INSERT INTO mention VALUES (%s, now() - interval '20 minutes', %s)", (eid, f"o{i}.com"))
        self.conn.commit()
        self.assertEqual(set(self._selected()), {"https://a/1", "https://a/6", "https://a/8", "https://a/9"})
