"""T26 (R28): country and region outages kept and placed, ASN rows dropped,
an unknown country skipped, and a longer revision updates in place."""
import json
from pathlib import Path

from newsradar import store
from newsradar.adapters import ioda
from tests.dbcase import DBCase

FIX = json.loads((Path(__file__).parent / "fixtures" / "ioda_events.json").read_text())


class T26Ioda(DBCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("TRUNCATE country_code, ioda_region, country_shape")
        self.conn.execute("INSERT INTO country_code VALUES ('LT','LTU','LH','Lithuania'), ('PT','PRT','PO','Portugal')")
        for fips, x in (("LH", 24.0), ("PO", -8.0)):
            self.conn.execute("""INSERT INTO country_shape VALUES (%s, %s,
                ST_Multi(ST_MakeEnvelope(%s, 38, %s, 56, 4326)))""", (fips, fips, x - 1, x + 1))
        self.conn.commit()

    def _get(self, url):
        assert "entityCode=3332" in url
        return json.dumps({"data": [{"code": "3332", "name": "Coimbra", "attrs": {"country_code": "PT", "ne_region_id": "1"}}]}).encode()

    def test_keep_place_and_revise(self):
        sid = store.source_id(self.conn, "ioda-outages")
        evs = list(ioda.parse(json.dumps(FIX).encode()))
        self.assertEqual(sorted(e.props["entity_type"] for e in evs), ["country", "country", "region"], "ASN row dropped")
        placed = ioda.resolve(self.conn, evs, get=self._get, pace=0)
        self.assertEqual(sorted(e.country for e in placed), ["LH", "PO"], "country/ZZ has no code and is skipped")
        coimbra = next(e for e in placed if e.props["entity_type"] == "region")
        self.assertEqual(coimbra.props["region_name"], "Coimbra")
        self.assertTrue(-9 <= coimbra.lon <= -7)
        store.insert_events(self.conn, sid, placed, update=True)
        self.conn.commit()
        # The outage grows: same key, longer duration, updated in place.
        FIX2 = json.loads(json.dumps(FIX))
        for e in FIX2["data"]:
            e["duration"] += 3600
        again = ioda.resolve(self.conn, list(ioda.parse(json.dumps(FIX2).encode())), get=self._get, pace=0)
        store.insert_events(self.conn, sid, again, update=True)
        self.conn.commit()
        self.assertEqual(self.one("SELECT count(*) FROM event WHERE source_id=%s", (sid,))[0], 2)
        lt = next(e for e in FIX["data"] if e["location"] == "country/LT")
        self.assertEqual(self.one("SELECT (props->>'duration_s')::int FROM event WHERE external_id LIKE %s", (f"%{lt['location']}%",))[0],
                         lt["duration"] + 3600)
