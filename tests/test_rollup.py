"""T3 (R4) rollups against hand-computed rows; T4 (R5) anomaly z-scores."""
from datetime import datetime, timedelta, timezone

import rollup
from tests.dbcase import DBCase

H0 = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


def _ev(conn, added, country, adm1, mentions, sources, n=1):
    for i in range(n):
        conn.execute("""INSERT INTO event (source_id, external_id, added_at, country, adm1, geom,
                        num_mentions, num_sources, num_articles)
                        VALUES (1, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(0,0),4326), %s, %s, %s)""",
                     (f"{added.isoformat()}-{country}-{adm1}-{i}", added, country, adm1, mentions, sources, mentions))


class T3Rollup(DBCase):
    def test_hourly_and_daily_match_hand_count(self):
        # events come from first sightings; mentions and sources from the buffer.
        _ev(self.conn, H0, "US", "USWA", 4, 2, n=3)                      # 3 events
        _ev(self.conn, H0 + timedelta(minutes=30), "US", "USCA", 10, 5)   # same hour, other ADM1
        _ev(self.conn, H0 + timedelta(hours=1), "US", "USWA", 1, 1)       # next hour
        _ev(self.conn, H0 + timedelta(days=1), "FR", "FR00", 7, 3)        # next day
        ids = dict(self.conn.execute("SELECT adm1 || added_at::text, min(id) FROM event GROUP BY 1").fetchall())
        wa, ca = ids["USWA" + str(H0).replace("+00:00", "+00")], ids["USCA" + str(H0 + timedelta(minutes=30)).replace("+00:00", "+00")]
        fr = ids["FR00" + str(H0 + timedelta(days=1)).replace("+00:00", "+00")]
        for at, eid, src in ((H0, wa, "a"), (H0, wa, "b"), (H0, ca, "a"),            # US H0: 3 mentions, 2 outlets
                             (H0 + timedelta(hours=1), wa, "c"),                     # US H0+1h
                             (H0 + timedelta(days=1), fr, "d"), (H0 + timedelta(days=1), fr, "d")):
            self.conn.execute("INSERT INTO mention VALUES (%s, %s, %s)", (eid, at, src))
        self.conn.commit()
        rollup.rollup(self.conn, H0 - timedelta(days=1), anomaly=False)
        self.assertEqual(self.one("SELECT events, mentions, sources FROM attention_hourly WHERE region_kind='country' AND region='US' AND hour=%s", (H0,)), (4, 3, 2))
        self.assertEqual(self.one("SELECT events, mentions, sources FROM attention_hourly WHERE region_kind='adm1' AND region='USWA' AND hour=%s", (H0,)), (3, 2, 2))
        self.assertEqual(self.one("SELECT events, mentions FROM attention_daily WHERE region_kind='country' AND region='US' AND day='2026-09-20'"), (5, 4))
        self.assertEqual(self.one("SELECT events, mentions, sources FROM attention_daily WHERE region_kind='country' AND region='FR' AND day='2026-09-21'"), (1, 2, 1))
        # 3 country rows (US H0, US H0+1h, FR +1d) and 4 adm1 rows; a re-run adds none.
        rollup.rollup(self.conn, H0 - timedelta(days=1), anomaly=False)
        self.assertEqual(self.one("SELECT count(*) FROM attention_hourly")[0], 7)


class T4Anomaly(DBCase):
    def _baseline(self, region, mentions_per_day, days=30, kind="country"):
        for d in range(1, days + 1):
            self.conn.execute("INSERT INTO attention_hourly VALUES (%s, %s, %s, 1, %s, 1)",
                              (kind, region, H0 - timedelta(days=d), mentions_per_day))

    def test_spike_scores_and_flat_does_not(self):
        self._baseline("US", 100)     # flat 100 every day at this hour
        self._baseline("FR", 100)
        self.conn.execute("INSERT INTO attention_hourly VALUES ('country','US',%s,1,500,1)", (H0,))  # 5x
        self.conn.execute("INSERT INTO attention_hourly VALUES ('country','FR',%s,1,100,1)", (H0,))  # flat
        self.conn.execute("INSERT INTO attention_hourly VALUES ('country','ZZ',%s,1,200,1)", (H0,))  # no history
        self.conn.commit()
        rollup.rollup(self.conn, H0 - timedelta(hours=1), H0 + timedelta(hours=1))
        us = self.one("SELECT baseline_mean, baseline_sd, z FROM attention_anomaly WHERE region='US'")
        fr = self.one("SELECT z FROM attention_anomaly WHERE region='FR'")
        zz = self.one("SELECT baseline_mean, z FROM attention_anomaly WHERE region='ZZ'")
        self.assertAlmostEqual(us[0], 100.0)
        self.assertAlmostEqual(us[1], 0.0)
        self.assertAlmostEqual(us[2], 400 / 10, places=3, msg="sd 0 falls back to sqrt(mean)=10")
        self.assertGreater(us[2], 3)
        self.assertLess(abs(fr[0]), 1)
        self.assertEqual(zz[0], 0.0)
        self.assertAlmostEqual(zz[1], 200.0, places=3, msg="never-seen region: floor of 1, so z = mentions")
