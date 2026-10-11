"""T50 to T56 (specs/state-youtube.md): channel list, list and detail
parsing, due checks, refresh, gone channels, the link lens, back-off,
configuration and the skill."""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsradar import statemedia as sm
from newsradar.adapters import youtube as yt
from tests.dbcase import DBCase

SKILL = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "youtube" / "SKILL.md"
NOW = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)
CH = "UCfiwzLy-8yKzIbsmZTzxDgw"
CH2 = "UCTHCOPwqNfZ0uiKOvFyhGwg"
BASE = "http://inv"


class FakeInvidious:
    """Each live channel lists videos v0..v{n-1}, v0 newest, one an hour back
    from NOW. `status` overrides a channel's list answer; `detail_fail` makes
    a video's detail call fail."""
    def __init__(self, n=3, status=None, detail_fail=(), views=100):
        self.urls, self.n, self.status, self.detail_fail, self.views = [], n, status or {}, set(detail_fail), views

    def __call__(self, url):
        self.urls.append(url)
        if "/api/v1/channels/" in url:
            ch = url.split("/api/v1/channels/")[1].split("/")[0]
            if ch in self.status:
                return self.status[ch]
            return 200, {"videos": [
                {"videoId": f"{ch[2:6]}v{i}", "title": f"title {i}", "author": "AlJazeera Arabic",
                 "published": int((NOW - timedelta(hours=i)).timestamp()), "lengthSeconds": 209 + i,
                 "viewCount": self.views, "liveNow": False, "isUpcoming": False} for i in range(self.n)]}
        vid = url.rsplit("/", 1)[1]
        if vid in self.detail_fail:
            return 500, {"error": "Could not extract"}
        return 200, {"videoId": vid, "title": f"full {vid}", "description": f"about {vid} https://english.news.cn/x",
                     "published": int(NOW.replace(hour=0).timestamp()), "keywords": ["news", "gulf"],
                     "viewCount": self.views + 7, "subCountText": "23.7M"}


class YTDB(DBCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("TRUNCATE yt_video, yt_channel, state_outlet_id, state_outlet RESTART IDENTITY CASCADE")
        o = self.conn.execute("""INSERT INTO state_outlet (country, name, typology, trajectory, smm_url, first_seen, last_seen)
                                 VALUES ('Qatar', 'Al Jazeera', 'CaPu', '{CaPu}', 'u', '2026-10-01', '2026-10-01'),
                                        ('China', 'Xinhua', 'SC', '{SC}', 'u', '2026-10-01', '2026-10-01')
                                 RETURNING id""").fetchall()
        self.aj, self.xinhua = o[0][0], o[1][0]
        self.conn.commit()

    def add_id(self, kind, value, outlet=None, origin="wikidata"):
        self.conn.execute("INSERT INTO state_outlet_id (outlet_id, kind, value, origin) VALUES (%s, %s, %s, %s)",
                          (outlet or self.aj, kind, value, origin))
        self.conn.commit()

    def collect(self, fake, now=NOW, ch=CH):
        r = yt.collect_channel(self.conn, BASE, ch, self.aj, fake, now, pause=0)
        self.conn.commit()
        return r


class T50Channels(YTDB):
    def test_non_uc_excluded_manual_included(self):
        self.add_id("youtube", CH)
        self.add_id("youtube", "@aljazeera")
        self.add_id("youtube", "UCshort")
        self.add_id("youtube", CH2, origin="manual")
        self.assertEqual({c for c, _ in yt.channels(self.conn)}, {CH, CH2})


class T51Parse(YTDB):
    def test_list_and_detail_fields(self):
        self.add_id("youtube", CH)
        self.assertEqual(self.collect(FakeInvidious(detail_fail={"fiwzv2"})), (3, 3))
        rows = {r[0]: r[1:] for r in self.conn.execute(
            "SELECT video_id, published_at, title, description, keywords, length_s, views FROM yt_video")}
        v0 = rows["fiwzv0"]
        self.assertEqual(v0, (NOW, "full fiwzv0", "about fiwzv0 https://english.news.cn/x", ["news", "gulf"], 209, 107))
        # A failed detail still stores the list's title and time, no description.
        self.assertEqual(rows["fiwzv2"], (NOW - timedelta(hours=2), "title 2", None, [], 211, 100))
        ch = self.one("SELECT title, subscribers, status FROM yt_channel")
        self.assertEqual(ch, ("AlJazeera Arabic", 23700000, "live"))


class T52Due(YTDB):
    def test_not_due_skipped_and_only_unseen_get_detail(self):
        self.add_id("youtube", CH)
        fake = FakeInvidious(n=3)
        self.collect(fake)
        self.assertEqual(len(fake.urls), 4)
        fake.urls.clear()
        self.assertIsNone(self.collect(fake, NOW + timedelta(minutes=30)))
        self.assertEqual(fake.urls, [])
        fake.n = 4
        self.assertEqual(self.collect(fake, NOW + timedelta(minutes=60)), (4, 1))
        self.assertEqual([u.rsplit("/", 1)[1] for u in fake.urls], ["videos", "fiwzv3"])


class T53Refresh(YTDB):
    def test_seen_again_updates_views_and_title(self):
        self.add_id("youtube", CH)
        self.collect(FakeInvidious())
        later = NOW + timedelta(hours=1)
        self.collect(FakeInvidious(views=5000), later)
        views, title, first, last, pub = self.one(
            "SELECT views, title, first_seen, last_seen, published_at FROM yt_video WHERE video_id = 'fiwzv0'")
        self.assertEqual((views, title, first, last, pub), (5000, "title 0", NOW, later, NOW))


class T54Gone(YTDB):
    def test_terminated_marked_and_retried_weekly(self):
        self.add_id("youtube", CH)
        err = {CH: (500, {"error": "This account has been terminated for a violation of YouTube's Terms of Service."})}
        fake = FakeInvidious(status=err)
        self.assertEqual(self.collect(fake), (0, 0))
        status, reason = self.one("SELECT status, reason FROM yt_channel")
        self.assertEqual(status, "gone")
        self.assertIn("terminated", reason)
        self.assertIsNone(self.collect(fake, NOW + timedelta(days=6)))
        self.assertEqual(len(fake.urls), 1)
        self.collect(fake, NOW + timedelta(days=8))
        self.assertEqual(len(fake.urls), 2)
        rows = sm.report(self.conn)["youtube"]
        self.assertEqual([r[3] for r in rows], [CH])
        self.assertTrue(rows[0][4].startswith("gone: This account has been terminated"))

    def test_other_error_leaves_status(self):
        self.add_id("youtube", CH)
        self.collect(FakeInvidious())
        fake = FakeInvidious(status={CH: (500, {"error": "Could not get channel info"})})
        with self.assertRaises(RuntimeError):
            self.collect(fake, NOW + timedelta(hours=2))
        self.conn.rollback()
        self.assertEqual(self.one("SELECT status FROM yt_channel")[0], "live")


class T55LinkLens(YTDB):
    def test_description_link_joins_registered_host(self):
        self.add_id("youtube", CH)
        self.add_id("host", "news.cn", self.xinhua)
        self.conn.execute("""INSERT INTO yt_channel (channel_id, outlet_id, status, checked_at) VALUES (%s, %s, 'live', now())""",
                          (CH, self.aj))
        self.conn.execute("""INSERT INTO yt_video (video_id, channel_id, published_at, title, description, first_seen, last_seen)
                             VALUES ('a', %s, now(), 't', 'see https://english.news.cn/a and https://fakenews.cn/b.', now(), now()),
                                    ('b', %s, now(), 't', NULL, now(), now())""", (CH, CH))
        self.conn.commit()
        rows = self.conn.execute("SELECT video_id, url, linked_outlet, channel_outlet_id FROM state_youtube_link").fetchall()
        self.assertEqual(rows, [("a", "https://english.news.cn/a", "Xinhua", self.aj)])


class T56Health(YTDB):
    def test_throttle_stops_run_and_logs(self):
        self.add_id("youtube", CH)
        self.add_id("youtube", CH2)
        fake = FakeInvidious(status={CH: (429, {})})
        errors = yt.run(self.conn, fake, NOW, pause=0, base=BASE)
        self.assertEqual(len(errors), 1)
        self.assertIn("429", errors[0])
        self.assertEqual(len(fake.urls), 1)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT file FROM fetch_log")], ["throttled/2026-10-11T22"])

    def test_one_fetch_log_row_per_collected_channel(self):
        self.add_id("youtube", CH)
        yt.run(self.conn, FakeInvidious(), NOW, pause=0, base=BASE)
        yt.run(self.conn, FakeInvidious(), NOW + timedelta(minutes=15), pause=0, base=BASE)
        rows = self.conn.execute("SELECT file, rows_seen, rows_kept FROM fetch_log").fetchall()
        self.assertEqual(rows, [(f"{CH}/2026-10-11T22", 3, 3)])

    def test_unset_url_makes_no_request(self):
        self.add_id("youtube", CH)
        fake = FakeInvidious()
        orig = yt.base_url
        yt.base_url = lambda: None
        try:
            self.assertEqual(yt.run(self.conn, fake, NOW, pause=0), [])
        finally:
            yt.base_url = orig
        self.assertEqual(fake.urls, [])

    def test_skill(self):
        text = SKILL.read_text()
        self.assertTrue(text.startswith("---\nname: youtube\n"))
        self.assertIn("/api/v1/channels/", text)
        self.assertIn("NEWSRADAR_INVIDIOUS_URL", text)


if __name__ == "__main__":
    unittest.main()
