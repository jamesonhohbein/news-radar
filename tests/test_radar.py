"""T27 (R29): with no token the adapter is skipped and inserts nothing; a
fixture response parses cause and type and places each location."""
from pathlib import Path
from unittest import mock

from newsradar import store
from newsradar.adapters import radar
from tests.dbcase import DBCase

BLOB = (Path(__file__).parent / "fixtures" / "radar_outages.json").read_bytes()


class T27Radar(DBCase):
    def test_no_token_is_skipped(self):
        with mock.patch.object(radar, "load_env", return_value={}):
            with self.assertRaises(radar.NotConfigured):
                radar.fetch()

    def test_parse_and_place(self):
        self.conn.execute("TRUNCATE country_code, country_shape")
        self.conn.execute("INSERT INTO country_code VALUES ('IQ','IRQ','IZ','Iraq'), ('US','USA','US','United States')")
        self.conn.execute("INSERT INTO country_shape VALUES ('IZ','Iraq', ST_Multi(ST_MakeEnvelope(40, 30, 46, 36, 4326)))")
        self.conn.commit()
        evs = list(radar.parse(BLOB))
        self.assertEqual([e.external_id for e in evs], ["1201:IQ", "1199:US", "1199:CA"])
        self.assertEqual((evs[0].props["cause"], evs[0].props["outage_type"], evs[0].props["end"]),
                         ("GOVERNMENT_DIRECTED", "NATIONWIDE", None))
        placed = radar.resolve(self.conn, evs)
        # US has a code but no shape here, CA has no code: only Iraq is placed.
        self.assertEqual([(e.country, 40 <= e.lon <= 46) for e in placed], [("IZ", True)])
        sid = self.one("SELECT id FROM source WHERE name = 'cloudflare-radar'")[0]
        store.insert_events(self.conn, sid, placed, update=True)
        self.conn.commit()
        self.assertEqual(self.one("SELECT props->>'cause' FROM primary_live WHERE source='radar'")[0], "GOVERNMENT_DIRECTED")
