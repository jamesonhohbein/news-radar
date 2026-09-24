"""T29 (R31): gazetteer matching in two scripts with word boundaries and the
country > city > division tie-break; a delete removes its URI; counts are
additive per place and bucket; no post text reaches the database."""
import json
from datetime import datetime, timezone

import bsky_stream
from newsradar import store
from newsradar.adapters import bluesky as bs
from tests.dbcase import DBCase

NAMES = [
    (1, "Paris", "city", 2_100_000), (2, "Paris", "city", 25_000),        # most populous wins
    (3, "Georgia", "country", 0), (4, "Georgia", "adm1", 0),               # country beats division
    (5, "東京", "city", 9_000_000), (5, "Tokyo", "city", 9_000_000),
    (6, "Nice", "city", 340_000),
    (7, "tokyo", "city", 1),                                                # lowercase transliteration: unusable
    (8, "Bay City", "city", 50_000, True), (8, "BBC", "city", 50_000, False),   # airport-code alternate: unusable
    (9, "Lai", "city", 20_000, True), (9, "ライ", "city", 20_000, False),         # 2-kana alternate: unusable
    (10, "ثون", "city", 40_000, False),                                          # Arabic: spaced script, bounded
]


def msg(op, t_us, rkey, text=None):
    c = {"collection": "app.bsky.feed.post", "operation": op, "rkey": rkey}
    if text is not None:
        c["record"] = {"text": text, "langs": ["en"]}
    return json.dumps({"did": "did:plc:x", "time_us": t_us, "kind": "commit", "commit": c})


class T29Bluesky(DBCase):
    def test_match(self):
        a = bs.build(NAMES)
        self.assertEqual(bs.match(a, "Flooding in Paris and Georgia tonight"), {1, 3})
        self.assertEqual(bs.match(a, "東京で地震がありました"), {5})
        self.assertEqual(bs.match(a, "Parisian food, nice weather"), set(), "word boundary and case")
        self.assertEqual(bs.match(a, "NiceParis"), set())
        self.assertEqual(bs.match(a, "BBC reports from Bay City"), {8})
        self.assertEqual(bs.match(a, "今日はライブに行く"), set(), "kana inside a word")
        self.assertEqual(bs.match(a, "في ثون اليوم"), {10})
        self.assertEqual(bs.match(a, "بثونا"), set(), "Arabic inside a word")

    def test_flush_counts_uris_delete_and_no_text(self):
        sid = store.source_id(self.conn, "bluesky-posts")
        a = bs.build(NAMES)
        t = int(datetime(2026, 9, 24, 7, 31, tzinfo=timezone.utc).timestamp() * 1e6)
        b, per_hour = bsky_stream.Batch(), bsky_stream.Counter()
        secret = "Protest in Paris right now, secret text"
        for i, text in enumerate([secret, "Paris again", "nothing here"]):
            bsky_stream.handle(a, b, per_hour, msg("create", t + i, f"r{i}", text))
        bsky_stream.flush(self.conn, sid, b, t + 2)
        b2 = bsky_stream.Batch()
        bsky_stream.handle(a, b2, per_hour, msg("create", t + 10, "r9", "Paris third"))
        bsky_stream.handle(a, b2, per_hour, msg("delete", t + 11, "r0"))
        bsky_stream.flush(self.conn, sid, b2, t + 11)
        self.assertEqual(self.one("SELECT posts FROM chatter_5min WHERE place_id=1")[0], 3)
        self.assertEqual(self.one("SELECT bucket FROM chatter_5min WHERE place_id=1")[0],
                         datetime(2026, 9, 24, 7, 30, tzinfo=timezone.utc))
        self.assertEqual(sorted(r[0] for r in self.conn.execute("SELECT uri FROM bsky_uri").fetchall()),
                         ["at://did:plc:x/app.bsky.feed.post/r1", "at://did:plc:x/app.bsky.feed.post/r9"])
        self.assertEqual(self.one("SELECT cursor FROM stream_cursor WHERE name='bluesky'")[0], str(t + 11))
        self.assertEqual(self.one("SELECT rows_seen, rows_kept FROM fetch_log WHERE source_id=%s", (sid,)), (4, 3))
        # No table holds the post text.
        for (table,) in self.conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'").fetchall():
            n = self.one(f"SELECT count(*) FROM {table} t WHERE t::text LIKE '%%secret text%%'")[0]
            self.assertEqual(n, 0, table)
