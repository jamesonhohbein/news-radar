"""T1, T2 (R2): the GDELT parser and idempotent ingest."""
import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path

from newsradar import store
from newsradar.adapters import gdelt
from tests.dbcase import DBCase

FIXTURE = Path(__file__).parent / "fixtures" / "export_sample.tsv"


def _rows():
    return list(csv.reader(io.StringIO(FIXTURE.read_text()), delimiter="\t", quoting=csv.QUOTE_NONE))


def _parsed():
    return list(gdelt.parse_rows(_rows()))


class T1Parse(DBCase.__mro__[1]):  # plain TestCase, no database
    def test_types_and_geo_skip(self):
        out = _parsed()
        self.assertEqual(len(out), 22)
        kept = [e for e, ok in out if ok]
        self.assertEqual(len(kept), 18, "4 fixture rows have ActionGeo_Type 0 and must be skipped")
        e = kept[0]
        self.assertEqual(e.external_id, "1323962893")
        self.assertEqual(e.occurred_on, date(2025, 9, 20))
        self.assertEqual(e.added_at, datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc))
        self.assertEqual((e.cameo_code, e.cameo_root, e.quad_class), ("0311", "03", 1))
        self.assertAlmostEqual(e.goldstein, 5.2)
        self.assertEqual((e.num_mentions, e.num_sources, e.num_articles), (4, 1, 4))
        self.assertEqual((e.country, e.adm1, e.lat, e.lon), ("CH", "CH", 35.0, 105.0))
        self.assertTrue(e.url.startswith("https://"))

    def test_short_row_is_skipped_not_fatal(self):
        out = list(gdelt.parse_rows([["only", "three", "cols"]]))
        self.assertEqual(out, [(None, False)])


class T2Idempotent(DBCase):
    def test_reingest_inserts_zero(self):
        sid = store.source_id(self.conn, "gdelt-events")
        evs = [e for e, ok in _parsed() if ok]
        first = store.insert_events(self.conn, sid, evs)
        self.conn.commit()
        second = store.insert_events(self.conn, sid, evs)
        self.conn.commit()
        self.assertEqual((first, second), (18, 0))
        self.assertEqual(self.one("SELECT count(*) FROM event")[0], 18)
        self.assertEqual(self.one("SELECT ST_Y(geom), ST_X(geom) FROM event WHERE external_id='1323962893'"), (35.0, 105.0))


class T2RecentFiles(DBCase.__mro__[1]):
    def test_slots_are_generated_not_listed(self):
        from datetime import timedelta
        now = datetime(2026, 9, 24, 8, 7, tzinfo=timezone.utc)
        files = gdelt.recent_files(now - timedelta(hours=1), gdelt.GKG, now=now)
        self.assertEqual(files, ["20260924070000.gkg.csv.zip", "20260924071500.gkg.csv.zip",
                                 "20260924073000.gkg.csv.zip", "20260924074500.gkg.csv.zip",
                                 "20260924080000.gkg.csv.zip"])
