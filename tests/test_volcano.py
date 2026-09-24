"""T24 (R26): a colour change is a new event; a notice at the same level
updates props; a volcano no longer listed drops off primary_live."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsradar import store
from newsradar.adapters import volcano
from tests.dbcase import DBCase

BLOB = (Path(__file__).parent / "fixtures" / "volcano_elevated.json").read_bytes()


class T24Volcano(DBCase):
    def test_colour_change_notice_update_and_drop(self):
        sid = store.source_id(self.conn, "usgs-volcanoes")
        evs = list(volcano.parse(BLOB))
        self.assertEqual(len(evs), 4)
        k = evs[0]
        self.assertEqual(k.external_id, "332010:20260907230041")
        self.assertEqual((k.props["color"], k.props["color_prev"]), ("ORANGE", "YELLOW"))
        self.assertEqual(k.added_at, datetime(2026, 9, 7, 23, 0, 41, tzinfo=timezone.utc))
        store.insert_events(self.conn, sid, evs, update=True)
        self.conn.commit()
        # Same level, newer notice: props change, no new row.
        data = json.loads(BLOB)
        data[0]["noticeSynopsis"] = "Newer notice"
        store.insert_events(self.conn, sid, list(volcano.parse(json.dumps(data).encode())), update=True)
        self.conn.commit()
        self.assertEqual(self.one("SELECT count(*) FROM event WHERE source_id=%s", (sid,))[0], 4)
        self.assertEqual(self.one("SELECT props->>'synopsis' FROM event WHERE external_id='332010:20260907230041'")[0], "Newer notice")
        # Colour change: RED from a new change date is a second Kilauea row.
        data[0].update(colorCode="RED", colorCodePrev="ORANGE", codeChangeDate="2026-09-24 01:00:00")
        # Great Sitkin has dropped out of the list, seen 4 h ago last.
        later = datetime.now(timezone.utc)
        store.insert_events(self.conn, sid, list(volcano.parse(json.dumps([data[0]] + data[2:]).encode(), now=later)), update=True)
        self.conn.execute("UPDATE event SET props = props || jsonb_build_object('seen', %s::text) WHERE external_id LIKE '311120:%%'",
                          ((later - timedelta(hours=4)).isoformat(),))
        self.conn.commit()
        self.assertEqual(self.one("SELECT count(*) FROM event WHERE source_id=%s AND external_id LIKE '332010:%%'", (sid,))[0], 2)
        live = {r[0] for r in self.conn.execute("SELECT geo_name FROM primary_live WHERE source='volcano'").fetchall()}
        self.assertNotIn("Great Sitkin", live)
        self.assertIn("Shishaldin", live)
