"""T34 (R36): the newest fatalities run is picked; every page is read; 36
months per country land with FIPS codes; an unmapped ISO code is counted and
dropped; the same run twice inserts nothing."""
import json
import unittest
from pathlib import Path

from newsradar import store
from newsradar.adapters import views
from tests.dbcase import DBCase

FIX = Path(__file__).parent / "fixtures"
RUNS = (FIX / "views_runs.json").read_bytes()
P1 = (FIX / "views_page1.json").read_bytes()
P2 = (FIX / "views_page2.json").read_bytes()
RUN = "fatalities003_2026_08_t01"
URL = f"{views.FEED}{RUN}/cm/sb?pagesize=10000"
BLOBS = {views.FEED: RUNS, URL: P1, URL + "&page=2": P2}


class T34Newest(unittest.TestCase):
    def test_newest_run(self):
        self.assertEqual(views.newest_run(json.loads(RUNS)["runs"]), RUN)


class T34Forecast(DBCase):
    def test_pages_mapping_and_idempotence(self):
        self.conn.execute("""INSERT INTO country_code (iso2, iso3, fips, name) VALUES
                             ('UA', 'UKR', 'UP', 'Ukraine'), ('SD', 'SDN', 'SU', 'Sudan')
                             ON CONFLICT DO NOTHING""")
        sid = store.source_id(self.conn, "views-forecast")
        line = views.load(self.conn, sid, store.log_fetch, fetch=BLOBS.__getitem__)
        self.conn.commit()
        self.assertIn("73 rows, 72 kept", line)
        per = dict(self.conn.execute("SELECT country, count(*) FROM conflict_forecast GROUP BY 1").fetchall())
        self.assertEqual(per, {"UP": 36, "SU": 36})
        self.assertEqual(self.one("SELECT fatalities FROM conflict_forecast WHERE country = 'UP' AND month = '2026-09-01'")[0], 4000.0)
        self.assertEqual(self.one("SELECT rows_seen, rows_kept FROM fetch_log WHERE file = %s", (RUN,)), (73, 72))
        self.assertIn("already loaded", views.load(self.conn, sid, store.log_fetch, fetch=BLOBS.__getitem__))
        self.conn.commit()
        self.assertEqual(self.one("SELECT count(*) FROM conflict_forecast")[0], 72)
