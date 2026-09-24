"""T22 (R24): a null-geometry alert is placed through its zones, fetched once
and cached; an Update marks what it references as superseded."""
import json
from pathlib import Path

from newsradar import store
from newsradar.adapters import nws
from tests.dbcase import DBCase

FIX = Path(__file__).parent / "fixtures"
ZONES = json.loads((FIX / "nws_zones.json").read_text())


class T22Nws(DBCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("TRUNCATE nws_zone")
        self.calls = []

    def _get(self, url):
        self.calls.append(url)
        return json.dumps(ZONES[url]).encode()

    def test_zones_place_alert_and_update_supersedes(self):
        sid = store.source_id(self.conn, "nws-alerts")
        evs = list(nws.parse((FIX / "nws_sample.json").read_bytes()))
        self.assertEqual([e.lat for e in evs], [None, None])
        placed = nws.resolve(self.conn, evs, get=self._get, pace=0)
        self.assertEqual(len(placed), 2)
        self.assertEqual(len(self.calls), 2, "only the zone-only alert's zones are fetched")
        a = placed[0]
        # Inside the union of both zones, and _geometry is not stored.
        self.assertTrue(-123.0 <= a.lon <= -121.0 and 48.6 <= a.lat <= 49.0)
        self.assertNotIn("_geometry", a.props)
        self.assertEqual(a.props["ugc"], ["WAC073", "WAZ503"])
        store.insert_events(self.conn, sid, placed, update=True)
        self.assertEqual(nws.after_insert(self.conn, sid), 1)
        self.conn.commit()
        self.assertEqual(self.one("SELECT superseded_by FROM event WHERE external_id='urn:oid:A'")[0], "urn:oid:B")
        self.assertIsNone(self.one("SELECT superseded_by FROM event WHERE external_id='urn:oid:B'")[0])
        # Second run: zones are cached, nothing fetched; marking is idempotent.
        nws.resolve(self.conn, list(nws.parse((FIX / "nws_sample.json").read_bytes())), get=self._get, pace=0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(nws.after_insert(self.conn, sid), 0)
        self.conn.rollback()
