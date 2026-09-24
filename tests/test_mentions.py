"""T17 (R18) Mentions ingest, T18 (R19) coverage-over-time attention, T19 (R20)
event_growth."""
import csv
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ingest
import rollup
from newsradar import store
from newsradar.adapters import gdelt
from tests.dbcase import DBCase
from tests.test_gdelt import _parsed

FIX = Path(__file__).parent / "fixtures" / "mentions_sample.tsv"
H0 = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("20260920150000.mentions.CSV", text)
    return buf.getvalue()


class T17Mentions(DBCase):
    def setUp(self):
        super().setUp()
        self.sid = store.source_id(self.conn, "gdelt-events")
        self.msid = store.source_id(self.conn, "gdelt-mentions")
        store.insert_events(self.conn, self.sid, [e for e, ok in _parsed() if ok])
        self.conn.commit()

    def test_parse(self):
        rows = list(gdelt.parse_mention_rows(csv.reader(io.StringIO(FIX.read_text()), delimiter="\t", quoting=csv.QUOTE_NONE)))
        self.assertEqual(len(rows), 7)
        self.assertIsNone(rows[-1], "malformed row is counted, not fatal")
        self.assertEqual(rows[0], ("1323962893", H0, "a.com"))

    def test_unknown_events_dropped_and_reingest_adds_nothing(self):
        blob = _zip(FIX.read_text())
        orig = ingest._fetch_or_none
        ingest._fetch_or_none = lambda f: blob
        try:
            files = ["20260920150000.mentions.CSV.zip"]
            first = ingest.load_mention_files(self.conn, self.msid, self.sid, files)
            second = ingest.load_mention_files(self.conn, self.msid, self.sid, files)
        finally:
            ingest._fetch_or_none = orig
        # 4 of 6 rows name events we hold; 1323962898 has no geo and 9999999999 is unknown.
        self.assertEqual(first, (1, 4))
        self.assertEqual(second, (0, 0))
        self.assertEqual(self.one("SELECT count(*) FROM mention")[0], 4)
        self.assertEqual(self.one("SELECT rows_seen, rows_kept FROM fetch_log WHERE source_id=%s", (self.msid,)), (7, 4))


def _event(conn, ext, added, country, adm1):
    return conn.execute("""INSERT INTO event (source_id, external_id, added_at, country, adm1, geom,
                           num_mentions, num_sources, num_articles)
                           VALUES (1, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(0,0),4326), 1, 1, 1)
                           RETURNING id""", (ext, added, country, adm1)).fetchone()[0]


def _mention(conn, eid, at, source):
    conn.execute("INSERT INTO mention VALUES (%s, %s, %s)", (eid, at, source))


class T18CoverageOverTime(DBCase):
    def test_running_story_lights_every_hour(self):
        eid = _event(self.conn, "x", H0, "US", "USWA")
        for h in range(5):
            _mention(self.conn, eid, H0 + timedelta(hours=h), "a.com")
            _mention(self.conn, eid, H0 + timedelta(hours=h, minutes=15), "a.com")
            _mention(self.conn, eid, H0 + timedelta(hours=h, minutes=30), f"s{h}.com")
        self.conn.commit()
        rollup.rollup(self.conn, H0 - timedelta(hours=1), anomaly=False)
        got = self.conn.execute("""SELECT hour, events, mentions, sources FROM attention_hourly
                                   WHERE region_kind='country' AND region='US' ORDER BY hour""").fetchall()
        self.assertEqual(len(got), 5)
        self.assertEqual(got[0][1:], (1, 3, 2), "first hour: 1 event, 3 mentions, 2 distinct outlets")
        for row in got[1:]:
            self.assertEqual(row[1:], (0, 3, 2), "later hours: no new event, coverage still counted")
        self.assertEqual(self.one("SELECT mentions, sources FROM attention_hourly WHERE region_kind='adm1' AND region='USWA' AND hour=%s", (H0 + timedelta(hours=4),)), (3, 2))
        self.assertEqual(self.one("SELECT count(*), sum(mentions) FROM mention_hourly WHERE event_id=%s", (eid,)), (5, 15))

    def test_hours_before_the_buffer_keep_their_mentions(self):
        self.conn.execute("INSERT INTO attention_hourly VALUES ('country','US',%s,4,99,9)", (H0 - timedelta(hours=3),))
        eid = _event(self.conn, "y", H0, "US", None)
        _mention(self.conn, eid, H0, "a.com")
        self.conn.commit()
        rollup.rollup(self.conn, H0 - timedelta(hours=6), anomaly=False)
        self.assertEqual(self.one("SELECT events, mentions, sources FROM attention_hourly WHERE region='US' AND hour=%s", (H0 - timedelta(hours=3),)), (4, 99, 9))


class T19Growth(DBCase):
    def test_rising_and_flat(self):
        now = datetime.now(timezone.utc)
        rising = _event(self.conn, "r", now - timedelta(hours=5), "US", None)
        flat = _event(self.conn, "f", now - timedelta(hours=5), "FR", None)
        for i in range(6):
            _mention(self.conn, rising, now - timedelta(minutes=10 + i), f"r{i}.com")
        _mention(self.conn, rising, now - timedelta(hours=4), "old.com")
        for h in range(5):
            _mention(self.conn, flat, now - timedelta(hours=h + 1, minutes=5), "same.com")
        self.conn.commit()
        r = self.one("SELECT mentions_1h, sources_1h, mentions_6h, sources_6h FROM event_growth WHERE event_id=%s", (rising,))
        f = self.one("SELECT mentions_1h, sources_1h, mentions_6h, sources_6h FROM event_growth WHERE event_id=%s", (flat,))
        self.assertEqual(r, (6, 6, 7, 7))
        self.assertEqual(f, (0, 0, 5, 1))
