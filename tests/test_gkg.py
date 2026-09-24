"""T21 (R22): GKG parse to articles, themes and every named place; type 1
never reaches ADM1; theme counts are one per article per region; a re-sent
article adds nothing."""
from datetime import date, datetime, timezone
from pathlib import Path

from newsradar import store
from newsradar.adapters import gdelt
from tests.dbcase import DBCase

LINES = (Path(__file__).parent / "fixtures" / "gkg_sample.tsv").read_text().splitlines()


class T21Parse(DBCase.__mro__[1]):
    def test_fields_locations_and_bad_rows(self):
        arts = list(gdelt.parse_gkg_lines(LINES + ["short\trow"]))
        self.assertIsNone(arts[-1])
        a, b, c = arts[:3]
        self.assertEqual(a.gkg_id, "20260924063000-2")
        self.assertEqual(a.added_at, datetime(2026, 9, 24, 6, 30, tzinfo=timezone.utc))
        self.assertEqual(a.site, "merimbulanewsweekly.com.au")
        self.assertAlmostEqual(a.tone, -0.7366, places=3)
        self.assertEqual(len(a.themes), 3)
        self.assertEqual([(l.loc_type, l.country, l.adm1) for l in a.locations],
                         [(4, "AS", "AS02"), (4, "AS", "AS02"), (1, "AS", None)])
        # White House and Washington share type and point: one location.
        self.assertEqual([(l.loc_type, l.country, l.adm1) for l in b.locations],
                         [(3, "US", "USDC"), (1, "IR", None), (2, "US", "USNY")])
        self.assertEqual(c.locations, ())
        for art in (a, b, c):
            self.assertEqual(len(art.themes), len(set(art.themes)))


class T21Store(DBCase):
    def test_articles_places_themes_and_idempotence(self):
        arts = [x for x in gdelt.parse_gkg_lines(LINES) if x]
        self.assertEqual(store.insert_gkg(self.conn, arts), 3)
        self.conn.commit()
        self.assertEqual(self.one("SELECT count(*) FROM gkg_location")[0], 6)
        self.assertEqual(self.one("SELECT count(*) FROM gkg_location WHERE loc_type = 1 AND adm1 IS NOT NULL")[0], 0)
        a, b = arts[0], arts[1]
        # Article a names Australia three times: each of its themes counts once for AS.
        t = a.themes[0]
        self.assertEqual(self.one("SELECT articles FROM theme_daily WHERE region_kind='country' AND region='AS' AND theme=%s AND day=%s",
                                  (t, date(2026, 9, 24)))[0], 1)
        self.assertEqual(self.one("SELECT articles FROM theme_daily WHERE region_kind='adm1' AND region='AS02' AND theme=%s", (t,))[0], 1)
        # Article b: US and IR at country level, USDC and USNY at ADM1, never IR at ADM1.
        tb = next(x for x in b.themes if x not in a.themes)
        self.assertEqual(self.one("SELECT count(*) FROM theme_daily WHERE theme=%s AND region_kind='country' AND region IN ('US','IR')", (tb,))[0], 2)
        self.assertEqual(self.one("SELECT count(*) FROM theme_daily WHERE theme=%s AND region_kind='adm1'", (tb,))[0], 2)
        self.assertEqual(self.one("SELECT articles FROM theme_hourly WHERE region='US' AND theme=%s", (tb,))[0], 1)
        # The article with no places adds no theme rows.
        before = self.one("SELECT sum(articles) FROM theme_daily")[0]
        self.assertEqual(store.insert_gkg(self.conn, arts), 0)
        self.conn.commit()
        self.assertEqual(self.one("SELECT sum(articles) FROM theme_daily")[0], before)
        self.assertEqual(self.one("SELECT count(*) FROM gkg_article")[0], 3)
