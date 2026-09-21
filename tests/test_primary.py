"""T13-T16 (R17): the USGS and GDACS adapters, reverse geocoding, upsert
semantics, and the attention exclusion."""
from datetime import timedelta, timezone
from pathlib import Path

import rollup
from newsradar import store
from newsradar.adapters import gdacs, usgs
from tests.dbcase import DBCase

FIX = Path(__file__).parent / "fixtures"


class T13Usgs(DBCase.__mro__[1]):
    def test_parse(self):
        evs = list(usgs.parse((FIX / "usgs_sample.json").read_bytes()))
        self.assertEqual(len(evs), 18)
        e = evs[0]
        self.assertEqual(e.external_id, "us7000tivi")
        self.assertEqual((e.lat, e.lon), (-36.2456, 177.7978))
        self.assertEqual(e.props["mag"], 4.7)
        self.assertAlmostEqual(e.props["depth_km"], 213.01)
        self.assertEqual(e.country, "")
        self.assertEqual(e.added_at.tzinfo, timezone.utc)
        self.assertTrue(e.props["title"].startswith("M 4.7"))


class T14Gdacs(DBCase.__mro__[1]):
    def test_parse(self):
        evs = list(gdacs.parse((FIX / "gdacs_sample.xml").read_bytes()))
        self.assertEqual(len(evs), 6)
        orange = [e for e in evs if e.props["alert"] == "Orange"]
        self.assertEqual(len(orange), 3)
        e = orange[0]
        self.assertTrue(e.external_id.startswith(e.props["event_type"]))
        self.assertEqual(e.props["kind"], gdacs.TYPES[e.props["event_type"]])
        self.assertIsNotNone(e.added_at.tzinfo)
        self.assertIn("modified", e.props)


class T15Geocode(DBCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # country_shape is loaded by scripts/load_countries.py, not schema.sql
        import json
        geo = json.loads((Path(__file__).resolve().parent.parent / "web/public/countries.geojson").read_text())
        for f in geo["features"]:
            if f["properties"]["fips"] in ("FR", "SP", "UK", "PP", "AS", "IT"):
                cls.conn.execute("INSERT INTO country_shape VALUES (%s, %s, ST_Multi(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)))) ON CONFLICT DO NOTHING",
                                 (f["properties"]["fips"], f["properties"]["name"], json.dumps(f["geometry"])))
        cls.conn.commit()

    def _ev(self, ext, lat, lon):
        self.conn.execute("""INSERT INTO event (source_id, external_id, added_at, country, geom, num_mentions, num_sources, num_articles)
                             VALUES ((SELECT id FROM source WHERE kind='usgs'), %s, now(), '', ST_SetSRID(ST_MakePoint(%s, %s), 4326), 1, 1, 1)""", (ext, lon, lat))

    def test_contains_nearest_and_ocean(self):
        self._ev("paris", 48.85, 2.35)        # inside France
        self._ev("channel", 49.6, -1.0)       # sea, ~40 km off Cherbourg: nearest FR
        self._ev("midatlantic", 30.0, -40.0)  # nowhere near land: stays ''
        self.conn.commit()
        sid = self.one("SELECT id FROM source WHERE kind='usgs'")[0]
        self.assertEqual(store.reverse_geocode(self.conn, sid), 3)
        self.conn.commit()
        got = dict(self.conn.execute("SELECT external_id, country FROM event").fetchall())
        self.assertEqual(got["paris"], "FR")
        self.assertEqual(got["channel"], "FR")
        self.assertEqual(got["midatlantic"], "")


class T16UpsertAndAttention(DBCase):
    def test_revision_updates_props_and_keeps_country(self):
        sid = self.one("SELECT id FROM source WHERE kind='usgs'")[0]
        evs = list(usgs.parse((FIX / "usgs_sample.json").read_bytes()))
        store.insert_events(self.conn, sid, evs, update=True)
        self.conn.execute("UPDATE event SET country = 'NZ' WHERE external_id = 'us7000tivi'")
        self.conn.commit()
        revised = evs[0].__class__(**{**evs[0].__dict__, "props": {**evs[0].props, "mag": 5.1}})
        store.insert_events(self.conn, sid, [revised], update=True)
        self.conn.commit()
        self.assertEqual(self.one("SELECT count(*) FROM event")[0], 18)
        self.assertEqual(self.one("SELECT (props->>'mag')::float, country FROM event WHERE external_id='us7000tivi'"), (5.1, "NZ"))

    def test_primaries_do_not_count_as_attention(self):
        for kind in ("usgs", "gdelt"):
            self.conn.execute("""INSERT INTO event (source_id, external_id, added_at, country, geom, num_mentions, num_sources, num_articles)
                                 VALUES ((SELECT id FROM source WHERE kind=%s), %s, now(), 'FR', ST_SetSRID(ST_MakePoint(2, 48), 4326), 10, 1, 1)""", (kind, kind))
        self.conn.commit()
        rollup.rollup(self.conn, __import__("datetime").datetime.now(timezone.utc) - timedelta(hours=2), anomaly=False)
        self.assertEqual(self.one("SELECT sum(mentions) FROM attention_hourly WHERE region='FR'")[0], 10)
