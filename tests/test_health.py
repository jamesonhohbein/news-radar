"""T30 (R32): source_health marks a source stale after three of its intervals
without a fetch that kept rows, whether the fetches stopped or came back empty."""
from tests.dbcase import DBCase


class T30SourceHealth(DBCase):
    def _log(self, name, file, minutes_ago, kept):
        self.conn.execute(
            """INSERT INTO fetch_log (source_id, file, fetched_at, rows_seen, rows_kept)
               SELECT id, %s, now() - make_interval(mins => %s), %s, %s FROM source WHERE name = %s""",
            (file, minutes_ago, kept, kept, name))

    def _stale(self, name):
        return self.one("SELECT stale FROM source_health WHERE name = %s", (name,))[0]

    def test_fresh_empty_and_stopped(self):
        self._log("gdelt-events", "a", 10, 500)
        self.assertFalse(self._stale("gdelt-events"))
        # Running, but every recent fetch came back empty.
        self._log("usgs-quakes", "b", 60, 12)
        for i, m in enumerate((45, 30, 15, 1)):
            self._log("usgs-quakes", f"e{i}", m, 0)
        self.assertTrue(self._stale("usgs-quakes"))
        # Stopped: last good fetch 50 minutes ago against a 15 minute interval.
        self._log("gdacs-alerts", "c", 50, 390)
        self.assertTrue(self._stale("gdacs-alerts"))
        self.conn.rollback()


class T35SlowSources(DBCase):
    def test_monthly_sources_expect_a_release_a_month(self):
        got = dict(self.conn.execute(
            "SELECT name, expect_every FROM source_health WHERE name IN ('ucdp-candidate', 'views-forecast')").fetchall())
        self.assertEqual(set(got), {"ucdp-candidate", "views-forecast"})
        # Stale is three intervals without rows: 36 days, one missed release.
        for every in got.values():
            self.assertEqual(3 * every.days, 36)
