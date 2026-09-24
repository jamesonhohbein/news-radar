"""T28 (R30): the stream keeps namespace-0 Wikipedia edits only; hourly
counts and distinct human editors are right; the resume id is stored with
the rows; hot pages get geolocated once."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rollup
import wiki_stream
from newsradar import store
from newsradar.adapters import wikipedia as wp
from tests.dbcase import DBCase

LINES = (Path(__file__).parent / "fixtures" / "wiki_sse.txt").read_text().splitlines(keepends=True)


class T28Wikipedia(DBCase):
    def test_filter_parse(self):
        events = list(wp.sse(LINES))
        self.assertEqual(len(events), 48)
        kept = [d for _, d in events if wp.keep(d)]
        self.assertEqual(len(kept), 7)
        self.assertTrue(all(d["server_name"].endswith(".wikipedia.org") and d["namespace"] == 0 for d in kept))
        r = wp.row(kept[0])
        self.assertEqual((r[0], r[2], r[5]), ("idwiki", "Kudeta Tailan 2006", True))
        self.assertEqual(len(r[4]), 12, "username stored only as a 12-char hash")

    def test_flush_rollup_cursor_and_geolocate(self):
        sid = store.source_id(self.conn, "wikipedia-edits")
        h = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        rows = [("enwiki", "en.wikipedia.org", "Quake", h + timedelta(minutes=m), u, bot, m == 0, 100 + m)
                for m, u, bot in ((0, "a", False), (5, "b", False), (9, "b", False), (12, "c", False), (20, "z", True))]
        self.assertEqual(wiki_stream.flush(self.conn, sid, rows, '[{"offset":1}]'), 5)
        self.assertEqual(wiki_stream.flush(self.conn, sid, rows, '[{"offset":2}]'), 0, "replay after resume adds nothing")
        self.assertEqual(self.one("SELECT cursor FROM stream_cursor WHERE name='wikipedia'")[0], '[{"offset":2}]')
        rollup.rollup(self.conn, h - timedelta(hours=1), anomaly=False)
        self.assertEqual(self.one("SELECT edits, editors, created FROM wiki_activity WHERE title='Quake' AND hour=%s", (h,)), (5, 3, True))
        # Three human editors: hot. Geolocated once, then cached.
        self.conn.execute("TRUNCATE country_shape")
        self.conn.execute("INSERT INTO country_shape VALUES ('US','US', ST_Multi(ST_MakeEnvelope(-125, 30, -110, 49, 4326)))")
        self.conn.commit()
        calls = []
        def lookup(server, titles):
            calls.append((server, tuple(titles)))
            return {t: (32.8, -117.0) for t in titles}
        self.assertEqual(wiki_stream.geolocate(self.conn, lookup), 1)
        self.assertEqual(wiki_stream.geolocate(self.conn, lookup), 0)
        self.assertEqual(calls, [("en.wikipedia.org", ("Quake",))])
        self.assertEqual(self.one("SELECT country FROM wiki_page WHERE title='Quake'")[0], "US")
        self.assertEqual(self.one("SELECT editors FROM wiki_geo_hourly WHERE country='US' AND hour=%s", (h,))[0], 3)
