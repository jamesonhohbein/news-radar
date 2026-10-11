"""T43 to T49 (specs/state-telegram.md): channel list, preview parsing,
paging, refresh, unreachable channels, the link lens, health and the skill."""
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsradar import statemedia as sm
from newsradar.adapters import telegram as tg
from tests.dbcase import DBCase

FIX = Path(__file__).parent / "fixtures"
PAGE = (FIX / "tg_presstv.html").read_text()
SKILL = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "telegram" / "SKILL.md"
NOW = datetime(2026, 10, 11, 22, 0, tzinfo=timezone.utc)


def page(handle: str, ids: range, start: datetime, step=timedelta(minutes=30)) -> bytes:
    """A synthetic preview page: posts with the given ids, oldest at start."""
    posts = "".join(
        f'<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" '
        f'data-post="{handle}/{i}"><div class="tgme_widget_message_text js-message_text" dir="auto">post {i}'
        f' <a href="https://english.news.cn/x/{i}">l</a></div>'
        f'<span class="tgme_widget_message_views">{i}</span>'
        f'<time datetime="{(start + step * n).isoformat()}" class="time">x</time></div></div>'
        for n, i in enumerate(ids))
    return f'<div class="tgme_channel_info_header_title"><span dir="auto">{handle}</span></div>{posts}'.encode()


class FakeTelegram:
    """Channel 'chan' holds posts 1..100, one every 30 minutes up to NOW."""
    def __init__(self, redirect=()):
        self.urls, self.redirect = [], set(redirect)
        self.views = 0

    def __call__(self, url):
        self.urls.append(url)
        handle = re.search(r"/s/([^?]+)", url).group(1)
        if handle in self.redirect:
            return 302, b""
        before = int(url.split("before=")[1]) if "before=" in url else 101
        ids = range(max(1, before - 20), before)
        start = NOW - timedelta(minutes=30) * (100 - ids.start)
        return 200, page(handle, ids, start)


class T44Parse(unittest.TestCase):
    def test_fixture_page(self):
        info, posts = tg.parse_page(PAGE)
        self.assertEqual(info, {"title": "Press TV", "subscribers": 92900})
        self.assertEqual(len(posts), 20)
        p = {x.post_id: x for x in posts}
        self.assertEqual([x.post_id for x in posts], sorted(p))
        self.assertEqual(p[210368].posted_at, datetime(2026, 10, 11, 17, 10, 49, tzinfo=timezone.utc))
        self.assertEqual(p[210368].views, 1930)
        self.assertTrue(p[210368].text.startswith("Yemeni Armed Forces:\n\nYemeni forces"))
        self.assertEqual(p[210368].media, ["photo"])
        self.assertEqual(p[210372].forwarded_from, "Unscripted_PTV")
        self.assertIn("https://www.presstv.co.uk/Detail/2026/10/11/778100/Israel-renews-airstrikes-and-artillery-shelling-in-southern-Lebanon-despite-ceasefire",
                      p[210373].links)

    def test_counts(self):
        self.assertEqual([tg.count(s) for s in ("34.9K", "1.2M", "812", "", None, "x")],
                         [34900, 1200000, 812, None, None, None])


class TGDB(DBCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("TRUNCATE tg_post, tg_channel, state_outlet_id, state_outlet RESTART IDENTITY CASCADE")
        o = self.conn.execute("""INSERT INTO state_outlet (country, name, typology, trajectory, smm_url, first_seen, last_seen)
                                 VALUES ('Russia', 'RT', 'SC', '{SC,SC,SC,SC,SC}', 'u', '2026-10-01', '2026-10-01'),
                                        ('China', 'Xinhua', 'SC', '{SC,SC,SC,SC,SC}', 'u', '2026-10-01', '2026-10-01')
                                 RETURNING id""").fetchall()
        self.rt, self.xinhua = o[0][0], o[1][0]
        self.conn.commit()

    def add_id(self, kind, value, outlet=None, origin="wikidata"):
        self.conn.execute("INSERT INTO state_outlet_id (outlet_id, kind, value, origin) VALUES (%s, %s, %s, %s)",
                          (outlet or self.rt, kind, value, origin))
        self.conn.commit()


class T43Channels(TGDB):
    def test_bots_and_invalid_handles_excluded(self):
        for h in ("chan", "ruptlybot", "uza yukla bot", "ab", "RT_DE"):
            self.add_id("telegram", h)
        self.add_id("host", "rt.com")
        self.add_id("telegram", "manualchan", origin="manual")
        self.assertEqual({h for h, _ in tg.channels(self.conn)}, {"RT_DE", "chan", "manualchan"})


class T45Paging(TGDB):
    def test_stops_at_stored_post_and_seven_days(self):
        self.add_id("telegram", "chan")
        fake = FakeTelegram()
        seen, new = tg.collect_channel(self.conn, "chan", self.rt, fake, NOW, pause=0)
        # 100 posts span 50 hours, inside 7 days: pages until the first post.
        self.assertEqual((seen, new), (100, 100))
        self.assertEqual(len(fake.urls), 5)
        fake.urls.clear()
        seen, new = tg.collect_channel(self.conn, "chan", self.rt, fake, NOW, pause=0)
        self.assertEqual((len(fake.urls), new), (1, 0))

    def test_seven_day_bound(self):
        self.add_id("telegram", "chan")
        fake = FakeTelegram()
        later = NOW + timedelta(days=7) - timedelta(hours=5)
        seen, _ = tg.collect_channel(self.conn, "chan", self.rt, fake, later, pause=0)
        # Posts 91..100 are inside the window; the first page reaches past it.
        self.assertEqual((len(fake.urls), seen), (1, 20))


class T46Refresh(TGDB):
    def test_seen_again_updates_views_keeps_first_seen(self):
        self.add_id("telegram", "chan")
        tg.collect_channel(self.conn, "chan", self.rt, FakeTelegram(), NOW, pause=0)
        self.conn.execute("UPDATE tg_post SET views = 0, text = 'old' WHERE post_id = 100")
        self.conn.commit()
        later = NOW + timedelta(minutes=15)
        tg.collect_channel(self.conn, "chan", self.rt, FakeTelegram(), later, pause=0)
        views, text, first, last = self.one("SELECT views, text, first_seen, last_seen FROM tg_post WHERE post_id = 100")
        self.assertEqual((views, first, last), (100, NOW, later))
        self.assertTrue(text.startswith("post 100"))


class T47Unreachable(TGDB):
    def test_redirect_marks_and_skips_for_a_day(self):
        self.add_id("telegram", "closed")
        fake = FakeTelegram(redirect={"closed"})
        self.assertEqual(tg.collect_channel(self.conn, "closed", self.rt, fake, NOW, pause=0), (0, 0))
        self.conn.commit()
        self.assertIs(self.one("SELECT preview FROM tg_channel WHERE handle = 'closed'")[0], False)
        tg.collect_channel(self.conn, "closed", self.rt, fake, NOW + timedelta(hours=23), pause=0)
        self.assertEqual(len(fake.urls), 1)
        tg.collect_channel(self.conn, "closed", self.rt, fake, NOW + timedelta(hours=25), pause=0)
        self.assertEqual(len(fake.urls), 2)
        rows = sm.report(self.conn)["telegram"]
        self.assertEqual([(r[3], r[4]) for r in rows], [("closed", "no preview")])


class T48LinkLens(TGDB):
    def test_link_joins_registered_host(self):
        self.add_id("telegram", "chan")
        self.add_id("host", "news.cn", self.xinhua)
        self.conn.execute("""INSERT INTO tg_channel (handle, outlet_id, preview, checked_at) VALUES ('chan', %s, true, now());
                             INSERT INTO tg_post (handle, post_id, posted_at, links, first_seen, last_seen) VALUES
                               ('chan', 1, now(), '{https://english.news.cn/a,https://fakenews.cn/b}', now(), now()),
                               ('chan', 2, now(), '{https://www.news.cn:443/c?x=1}', now(), now())""" % self.rt)
        self.conn.commit()
        rows = self.conn.execute("SELECT post_id, url, linked_outlet, channel_outlet_id FROM state_telegram_link ORDER BY 1").fetchall()
        self.assertEqual(rows, [(1, "https://english.news.cn/a", "Xinhua", self.rt),
                                (2, "https://www.news.cn:443/c?x=1", "Xinhua", self.rt)])


class T49Health(TGDB):
    def test_one_fetch_log_row_per_channel_and_failure_isolated(self):
        self.add_id("telegram", "chan")
        self.add_id("telegram", "broken")
        fake = FakeTelegram()

        def fetch(url):
            return (500, b"") if "/broken" in url else fake(url)
        errors = tg.run(self.conn, fetch, NOW, pause=0)
        self.assertEqual(len(errors), 1)
        self.assertIn("broken", errors[0])
        rows = self.conn.execute("SELECT file, rows_seen, rows_kept FROM fetch_log ORDER BY 1").fetchall()
        self.assertEqual(rows, [("chan/2026-10-11T22", 100, 100)])

    def test_skill(self):
        text = SKILL.read_text()
        self.assertTrue(text.startswith("---\nname: telegram\n"))
        self.assertIn(tg.PREVIEW, text)


if __name__ == "__main__":
    unittest.main()
