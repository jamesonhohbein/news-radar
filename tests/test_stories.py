"""T20 (R21): syndicated reprints collapse into one story; outlets and sites
are counted across the story, not per URL."""
from tests.dbcase import DBCase


class T20Syndication(DBCase):
    def test_four_headlines_three_sites_three_stories(self):
        self.conn.execute("TRUNCATE story")
        heads = [
            ("https://x.co.uk/1", "x.co.uk", "Burnham meets von der Leyen - Wiltshire Times"),
            ("https://y.co.uk/1", "y.co.uk", "Burnham meets von der Leyen | The Westmorland Gazette"),
            ("https://y.co.uk/2", "y.co.uk", "Floods close the A66"),
            ("https://z.com/1", "z.com", "Xi arrives in Washington"),
        ]
        for i, (url, site, title) in enumerate(heads):
            eid = self.conn.execute("""INSERT INTO event (source_id, external_id, added_at, country, geom, num_mentions, num_sources, num_articles, url)
                                       VALUES (1, %s, now() - interval '2 hours', 'UK', ST_SetSRID(ST_MakePoint(0,0),4326), 1, 1, 1, %s)
                                       RETURNING id""", (str(i), url)).fetchone()[0]
            self.conn.execute("INSERT INTO story (url, title, site, status) VALUES (%s, %s, %s, 'ok')", (url, title, site))
            for o in range(i + 1):
                self.conn.execute("INSERT INTO mention VALUES (%s, now() - interval '30 minutes', %s)", (eid, f"{site}-o{o}"))
        self.conn.commit()
        got = {r[0]: r[1:] for r in self.conn.execute("SELECT skey, outlets, sites FROM top_stories(24, 10)").fetchall()}
        self.assertEqual(len(got), 3)
        # Burnham: URL 0 (1 outlet) + URL 1 (2 outlets), two sites.
        self.assertEqual(got["burnham meets von der leyen"], (3, 2))
        self.assertEqual(got["floods close the a66"], (3, 1))
        self.assertEqual(got["xi arrives in washington"], (4, 1))
        first = self.conn.execute("SELECT skey FROM top_stories(24, 1)").fetchone()[0]
        self.assertEqual(first, "xi arrives in washington")
