"""T32, T33 (R35): UCDP candidate discovery finds only candidate CSVs, oldest
release first; events carry their props, keep first-sight added_at and are
revised in place by a later file; non-Clear rows are stored."""
import csv
import io
import unittest
from datetime import datetime, timezone
from pathlib import Path

from newsradar import store
from newsradar.adapters import ucdp
from tests.dbcase import DBCase

FIX = Path(__file__).parent / "fixtures"
HTML = (FIX / "ucdp_downloads.html").read_text()
CSV = (FIX / "ucdp_candidate.csv").read_bytes()
BASE = "https://ucdp.uu.se/downloads/candidateged/"


def _revised(blob: bytes, best: str) -> bytes:
    rows = list(csv.DictReader(io.StringIO(blob.decode("utf-8-sig"))))
    rows[0]["best"] = best
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return out.getvalue().encode()


class T32Discovery(unittest.TestCase):
    def test_only_candidate_csvs_oldest_first(self):
        files = ucdp.candidate_files(HTML)
        self.assertEqual([n for _, n in files],
                         ["GEDEvent_v26_01_26_06.csv", "GEDEvent_v26_0_7.csv", "GEDEvent_v26_0_8.csv"])
        self.assertTrue(all(u == BASE + n for u, n in files))


class T33Events(DBCase):
    def test_parse_upsert_keeps_added_at_and_non_clear(self):
        sid = store.source_id(self.conn, "ucdp-candidate")
        first = datetime(2026, 9, 1, tzinfo=timezone.utc)
        evs = list(ucdp.parse(CSV, first))
        self.assertEqual(len(evs), 2)
        e = evs[0]
        self.assertEqual(e.external_id, "637348")
        self.assertEqual(e.added_at, first)
        for k in ("type_of_violence", "conflict_name", "dyad_name", "side_a", "side_b", "best", "low", "high",
                  "deaths_civilians", "where_prec", "date_prec", "date_end", "code_status", "source_office", "source_headline"):
            self.assertIn(k, e.props)
        self.assertEqual((e.props["best"], e.props["type_of_violence"]), (1, 3))

        # Two releases through load(): the second revises best and must not move added_at.
        html = HTML.replace("GEDEvent_v26_01_26_06.csv", "x").replace("GEDEvent_v26_0_7.csv", "y")
        blobs = {ucdp.FEED: html.encode(), BASE + "GEDEvent_v26_0_8.csv": CSV}
        ucdp.load(self.conn, sid, store.log_fetch, fetch=blobs.__getitem__)
        self.conn.execute("UPDATE event SET added_at = %s WHERE source_id = %s", (first, sid))
        self.conn.commit()
        blobs[ucdp.FEED] = (html + f'<a href="{BASE}GEDEvent_v26_0_9.csv">').encode()
        blobs[BASE + "GEDEvent_v26_0_9.csv"] = _revised(CSV, "7")
        line = ucdp.load(self.conn, sid, store.log_fetch, fetch=blobs.__getitem__)
        self.conn.commit()
        self.assertIn("1 new", line)
        self.assertEqual(self.one("SELECT count(*) FROM event WHERE source_id = %s", (sid,))[0], 2)
        best, added = self.one("SELECT (props->>'best')::int, added_at FROM event WHERE external_id = '637348'")
        self.assertEqual((best, added), (7, first))
        self.assertEqual(self.one("SELECT props->>'code_status' FROM event WHERE external_id = '638586'")[0], "Check dyad")
        # Nothing new: one poll row, no reload.
        self.assertIn("0 new", ucdp.load(self.conn, sid, store.log_fetch, fetch=blobs.__getitem__))
