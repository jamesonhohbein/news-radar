"""T23 (R25): an Atom bulletin parses to a point event with its category and
magnitude; the same uuid twice inserts once; primary_live shows only
non-Information bulletins from the last 24 h."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsradar import store
from newsradar.adapters import tsunami
from tests.dbcase import DBCase

FIX = Path(__file__).parent / "fixtures"
BLOB = (FIX / "tsunami_paaq.xml").read_bytes() + tsunami.SEP + (FIX / "tsunami_pheb.xml").read_bytes()


class T23Tsunami(DBCase):
    def test_parse_idempotent_and_live_rule(self):
        evs = list(tsunami.parse(BLOB))
        self.assertEqual(len(evs), 2)
        a = evs[0]
        self.assertEqual(a.external_id, "urn:uuid:16b1f03a-4157-417b-abd2-22a82dbd3379")
        self.assertEqual((a.lat, a.lon), (52.81, -171.341))
        self.assertEqual((a.props["centre"], a.props["category"], a.props["magnitude"]), ("PAAQ", "Information", 6.3))
        self.assertEqual(a.added_at, datetime(2026, 9, 17, 14, 23, 45, tzinfo=timezone.utc))
        sid = store.source_id(self.conn, "tsunami-bulletins")
        now = datetime.now(timezone.utc)
        # Make both fresh; one a Warning, one Information.
        fresh = [replace(evs[0], added_at=now - timedelta(hours=1), props={**evs[0].props, "category": "Warning"}),
                 replace(evs[1], added_at=now - timedelta(hours=1))]
        self.assertEqual(store.insert_events(self.conn, sid, fresh, update=True), 2)
        self.conn.commit()  # staging is ON COMMIT DROP; see CLAUDE.md
        store.insert_events(self.conn, sid, fresh, update=True)
        self.conn.commit()
        self.assertEqual(self.one("SELECT count(*) FROM event WHERE source_id=%s", (sid,))[0], 2)
        live = [r[0] for r in self.conn.execute("SELECT props->>'category' FROM primary_live WHERE source='tsunami'").fetchall()]
        self.assertEqual(live, ["Warning"])
