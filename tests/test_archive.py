"""T57-T65, T68 (specs/archive.md): archive before prune."""
import contextlib
import hashlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pyarrow.parquet as pq

import rollup
from newsradar import archive
from tests.dbcase import DBCase

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
OLD = datetime(2026, 7, 1, 6, 30, tzinfo=timezone.utc)      # past every window but theme_daily's
ANCIENT = datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc)    # past theme_daily's 180 d
NEW = NOW - timedelta(days=1)                                # inside every window


def sid(conn, name):
    return conn.execute("SELECT id FROM source WHERE name = %s", (name,)).fetchone()[0]


class ArchiveCase(DBCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.arch = Path(self.tmp.name) / "archive"
        self.scratch = Path(self.tmp.name) / "scratch"
        self.arch.mkdir()
        self.scratch.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def prune(self, **kw):
        return rollup.prune(self.conn, kw.pop("archive_dir", self.arch), self.scratch, now=kw.pop("now", NOW))

    def ev(self, at, ext, source="gdelt-events", props=None):
        return self.conn.execute(
            """INSERT INTO event (source_id, external_id, added_at, country, geom, num_mentions,
                                  num_sources, num_articles, tone, props)
               VALUES (%s, %s, %s, 'US', ST_SetSRID(ST_MakePoint(-122.5, 48.75), 4326), 3, 2, 1, 1.1, %s)
               RETURNING id""",
            (sid(self.conn, source), ext, at, props)).fetchone()[0]

    def fixture(self, at):
        """One row in every archived table at `at`."""
        c = self.conn
        self.ev(at, f"e-{at.isoformat()}", props='{"a": [1, "x"], "b": null}')
        c.execute("INSERT INTO theme_hourly VALUES ('US', 'TAX', %s, 4)", (at,))
        c.execute("INSERT INTO mention_hourly VALUES (1, %s, 5, 2)", (at,))
        c.execute("INSERT INTO attention_hourly VALUES ('country', 'US', %s, 1, 2, 3)", (at,))
        c.execute("INSERT INTO wiki_activity VALUES ('en', 'Page', %s, 3, 2, false)", (at,))
        c.execute("INSERT INTO chatter_hourly VALUES ('bluesky', 7, %s, 9)", (at,))
        aid = c.execute("""INSERT INTO gkg_article (gkg_id, added_at, url, site, tone, themes)
                           VALUES (%s, %s, 'u', 's', -2.5, ARRAY['TAX', 'ENV'])
                           RETURNING id""", (f"g-{at.isoformat()}", at)).fetchone()[0]
        c.execute("""INSERT INTO gkg_location VALUES (%s, 1, 'US', NULL, ST_SetSRID(ST_MakePoint(1.5, 2.5), 4326)),
                                                    (%s, 2, 'US', 'USWA', ST_SetSRID(ST_MakePoint(3, 4), 4326))""", (aid, aid))
        c.execute("""INSERT INTO firms_detection (satellite, lat, lon, acq_at, frp, confidence, daynight, bright_ti4, geom)
                     VALUES ('N', 10, 20, %s, 1.5, 'high', 'D', 300.5, ST_SetSRID(ST_MakePoint(20, 10), 4326))""", (at,))
        c.execute("INSERT INTO theme_daily VALUES ('country', 'US', 'TAX', %s, 4)", (at.date(),))
        c.commit()

    def count(self, table):
        return self.one(f"SELECT count(*) FROM {table}")[0]

    def files(self):
        return sorted(str(p.relative_to(self.arch)) for p in self.arch.rglob("*") if p.is_file())


class T57ArchiveThenDelete(ArchiveCase):
    def test_old_rows_archived_and_deleted_new_rows_stay(self):
        self.fixture(OLD)
        self.fixture(NEW)
        self.conn.execute("INSERT INTO theme_daily VALUES ('country', 'US', 'ENV', %s, 1)", (ANCIENT.date(),))
        self.conn.commit()
        out = self.prune()
        self.assertNotIn("failed", out)
        day = OLD.date().isoformat()
        for t in ("event", "theme_hourly", "mention_hourly", "attention_hourly", "wiki_activity",
                  "chatter_hourly", "gkg_article", "gkg_location", "firms_detection"):
            f = self.arch / t / "2026" / f"{day}.0.parquet"
            self.assertTrue(f.exists(), t)
            self.assertEqual(pq.ParquetFile(f).metadata.num_rows, 2 if t == "gkg_location" else 1, t)
        # theme_daily: OLD is inside 180 d, ANCIENT is not.
        self.assertTrue((self.arch / "theme_daily" / "2026" / "2026-03-01.0.parquet").exists())
        self.assertEqual(self.count("theme_daily"), 2)
        for t in ("event", "theme_hourly", "mention_hourly", "attention_hourly", "wiki_activity",
                  "chatter_hourly", "gkg_article", "firms_detection"):
            self.assertEqual(self.count(t), 1, t)   # only the NEW row is left
        self.assertEqual(self.count("gkg_location"), 2)
        self.assertEqual(out["event"], 1)

    def test_nothing_due_writes_nothing(self):
        self.fixture(NEW)
        self.prune()
        self.assertEqual(self.files(), [])

    def test_partial_day_waits(self):
        # Inside the last, partly-due UTC day: not archived until the whole day is due.
        cut = NOW - timedelta(days=7)
        self.conn.execute("INSERT INTO theme_hourly VALUES ('US', 'TAX', %s, 4)", (cut - timedelta(hours=1),))
        self.conn.commit()
        self.prune()
        self.assertEqual(self.count("theme_hourly"), 1)


class T58ShortFile(ArchiveCase):
    def test_dropped_row_blocks_delete(self):
        self.fixture(OLD)
        self.conn.execute("INSERT INTO theme_hourly VALUES ('FR', 'TAX', %s, 1)", (OLD,))
        self.conn.commit()
        real = archive._to_arrow

        def short(batch, schema):
            t = real(batch, schema)
            return t.slice(0, t.num_rows - 1) if schema.names == ["region", "theme", "hour", "articles"] else t

        with mock.patch.object(archive, "_to_arrow", short):
            out = self.prune()
        self.assertIn("theme_hourly", out["failed"])
        self.assertEqual(self.count("theme_hourly"), 2)
        self.assertFalse((self.arch / "theme_hourly").exists())
        self.assertEqual(self.count("event"), 0)    # other tables went ahead

    def test_main_exits_nonzero(self):
        self.fixture(OLD)
        with mock.patch.object(archive, "_export", side_effect=RuntimeError("boom")), \
             mock.patch.object(rollup, "connect", lambda: contextlib.nullcontext(self.conn)), \
             mock.patch.dict(os.environ, {"NEWSRADAR_ARCHIVE_DIR": str(self.arch),
                                          "NEWSRADAR_ARCHIVE_SCRATCH": str(self.scratch)}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                rollup.main([])
        self.assertEqual(cm.exception.code, 1)


class T59PerTable(ArchiveCase):
    def test_event_failure_does_not_block_theme_hourly(self):
        self.fixture(OLD)
        real = archive._export

        def fail_event(conn, table, *a):
            if table == "event":
                raise RuntimeError("injected")
            return real(conn, table, *a)

        with mock.patch.object(archive, "_export", fail_event):
            out = self.prune()
        self.assertEqual(list(out["failed"]), ["event"])
        self.assertEqual(self.count("event"), 1)
        self.assertEqual(self.count("theme_hourly"), 0)
        self.assertTrue((self.arch / "theme_hourly" / "2026" / "2026-07-01.0.parquet").exists())
        self.assertFalse((self.arch / "event").exists())


class T60LateRows(ArchiveCase):
    def test_late_row_gets_next_n(self):
        self.fixture(OLD)
        self.prune()
        f0 = self.arch / "theme_hourly" / "2026" / "2026-07-01.0.parquet"
        h0 = hashlib.sha256(f0.read_bytes()).hexdigest()
        self.conn.execute("INSERT INTO theme_hourly VALUES ('DE', 'LATE', %s, 2)", (OLD + timedelta(hours=3),))
        self.conn.commit()
        self.prune()
        self.assertEqual(hashlib.sha256(f0.read_bytes()).hexdigest(), h0)
        f1 = f0.with_name("2026-07-01.1.parquet")
        rows = pq.read_table(f1).to_pylist()
        self.assertEqual([(r["region"], r["theme"]) for r in rows], [("DE", "LATE")])
        self.assertEqual(self.count("theme_hourly"), 0)


class T61CrashBeforeMove(ArchiveCase):
    def test_crash_leaves_archive_empty_and_rows(self):
        self.fixture(OLD)
        with mock.patch.object(archive, "_move_into", side_effect=OSError("crash")):
            out = self.prune()
        self.assertEqual(len(out["failed"]), 8)   # theme_daily has nothing due
        self.assertEqual(self.files(), [])
        self.assertEqual(self.count("event"), 1)
        self.assertEqual(self.count("gkg_location"), 2)


class T62NoArchiveDir(ArchiveCase):
    def test_unset_keeps_archived_tables_prunes_buffers(self):
        self.fixture(OLD)
        self.conn.execute("INSERT INTO chatter_5min (source, place_id, bucket, posts) VALUES ('bluesky', 7, %s, 1)", (OLD,))
        self.conn.commit()
        out = self.prune(archive_dir=None)
        self.assertIn("skipped", out["archive"])
        self.assertEqual(self.count("chatter_5min"), 0)
        for t in ("event", "theme_hourly", "gkg_article", "firms_detection", "theme_daily"):
            self.assertEqual(self.count(t), 1, t)
        self.assertEqual(self.files(), [])


class T63NoPrune(ArchiveCase):
    def test_no_prune_writes_and_deletes_nothing(self):
        self.fixture(OLD)
        with mock.patch.object(rollup, "connect", lambda: contextlib.nullcontext(self.conn)), \
             mock.patch.dict(os.environ, {"NEWSRADAR_ARCHIVE_DIR": str(self.arch)}), \
             contextlib.redirect_stdout(io.StringIO()):
            rollup.main(["--no-prune"])
        self.assertEqual(self.files(), [])
        self.assertEqual(self.count("event"), 1)
        self.assertEqual(self.count("theme_hourly"), 1)


class T64Windows(unittest.TestCase):
    def test_windows_match_decision_5(self):
        self.assertEqual(rollup.ARCHIVE_WINDOWS, {
            "event": 60, "theme_hourly": 7, "mention_hourly": 7, "attention_hourly": 35,
            "wiki_activity": 30, "chatter_hourly": 30, "gkg_article": 30, "firms_detection": 30,
            "theme_daily": 180})
        self.assertEqual([t.name for t in archive.tables(rollup.ARCHIVE_WINDOWS)],
                         ["event", "theme_hourly", "mention_hourly", "attention_hourly", "wiki_activity",
                          "chatter_hourly", "gkg_article", "firms_detection", "theme_daily"])


class T65RoundTrip(ArchiveCase):
    def test_query_returns_what_postgres_did(self):
        self.fixture(OLD)
        pairs = [  # (Postgres before, archive after)
            ("""SELECT id, external_id, added_at, tone, ST_X(geom), ST_Y(geom), props::text FROM event ORDER BY id""",
             "SELECT id, external_id, added_at, tone, lon, lat, props FROM event ORDER BY id"),
            ("SELECT id, gkg_id, added_at, tone, themes FROM gkg_article ORDER BY id",) * 2,
            ("SELECT article_id, loc_type, adm1, ST_X(geom), ST_Y(geom) FROM gkg_location ORDER BY loc_type",
             "SELECT article_id, loc_type, adm1, lon, lat FROM gkg_location ORDER BY loc_type"),
        ]
        fire_sql = "SELECT id, lat, lon, acq_at, frp, geom_lon, geom_lat FROM firms_detection ORDER BY id"
        before = {a: self.conn.execute(pg).fetchall() for pg, a in pairs}
        fires = self.conn.execute("SELECT id, lat, lon, acq_at, frp, ST_X(geom), ST_Y(geom) FROM firms_detection").fetchall()
        td = self.conn.execute("SELECT * FROM theme_daily").fetchall()
        self.prune(now=NOW + timedelta(days=200))
        self.assertEqual(self.count("event"), 0)
        for q, rows in before.items():
            self.assertEqual([tuple(r) for r in archive.query(q, self.arch)[1]], rows, q)
        self.assertEqual(archive.query(fire_sql, self.arch)[1], fires)
        self.assertEqual(archive.query("SELECT * FROM theme_daily", self.arch)[1], td)
        cols, _ = archive.query("SELECT * FROM event LIMIT 0", self.arch)
        self.assertIn("lon", cols)
        self.assertNotIn("geom", cols)

    def test_tables_without_files_have_no_view(self):
        self.assertEqual(archive.query("SELECT 1", self.arch)[1], [(1,)])
        with self.assertRaises(Exception):
            archive.query("SELECT * FROM event", self.arch)


class T68FeedEventsKept(ArchiveCase):
    def test_gdacs_and_volcano_neither_archived_nor_deleted(self):
        self.ev(OLD, "DR1", source="gdacs-alerts")
        self.ev(OLD, "V1", source="usgs-volcanoes")
        self.ev(OLD, "Q1", source="usgs-quakes")
        self.conn.commit()
        out = self.prune()
        self.assertEqual(out["event"], 1)
        left = {r[0] for r in self.conn.execute("SELECT external_id FROM event").fetchall()}
        self.assertEqual(left, {"DR1", "V1"})
        _, rows = archive.query("SELECT external_id FROM event", self.arch)
        self.assertEqual(rows, [("Q1",)])


if __name__ == "__main__":
    unittest.main()
