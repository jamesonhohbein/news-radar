"""T25 (R27): detections 500 m apart form one fire, 5 km apart two; a later
detection next to a live fire extends it rather than starting a new one."""
from datetime import datetime, timedelta, timezone

from newsradar import store
from newsradar.adapters import firms
from tests.dbcase import DBCase

LAT = 38.5
KM_LON = 1 / (111.32 * 0.7826)   # degrees of longitude per km at 38.5 N


class T25Firms(DBCase):
    def _rows(self, at, lons, sat="N"):
        return [(sat, LAT, lon, at, 5.0, "nominal", "D", 330.0) for lon in lons]

    def test_cluster_extend_and_live_rule(self):
        sid = store.source_id(self.conn, "firms-fires")
        t0 = datetime.now(timezone.utc) - timedelta(hours=30)
        a = -120.0
        # A: two pixels 500 m apart. B: 5 km east of A.
        firms.insert(self.conn, iter(self._rows(t0, [a, a + 0.5 * KM_LON, a + 5 * KM_LON])))
        self.conn.commit()
        out = firms.cluster(self.conn, sid)
        self.conn.commit()
        self.assertEqual(out["new_fires"], 2)
        self.assertEqual(self.one("SELECT count(DISTINCT fire_id) FROM firms_detection")[0], 2)
        fire_a = self.one("SELECT fire_id FROM firms_detection WHERE lon = %s", (a,))[0]
        # Next pass, a day later: a pixel 700 m west of A joins A.
        firms.insert(self.conn, iter(self._rows(t0 + timedelta(hours=24), [a - 0.7 * KM_LON], sat="N20")))
        self.conn.commit()
        out = firms.cluster(self.conn, sid)
        self.conn.commit()
        self.assertEqual((out["joined"], out["new_fires"]), (1, 0))
        self.assertEqual(self.one("SELECT fire_id FROM firms_detection WHERE satellite='N20'")[0], fire_a)
        self.assertEqual(self.one("SELECT (props->>'detections')::int FROM event WHERE id=%s", (fire_a,))[0], 3)
        # Re-inserting the same rows adds nothing.
        self.assertEqual(firms.insert(self.conn, iter(self._rows(t0, [a])))[1], 0)
        self.conn.commit()
        # Below 20 detections: not on the map.
        self.assertEqual(self.one("SELECT count(*) FROM primary_live WHERE source='firms'")[0], 0)

    def test_parse(self):
        blob = (b"latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight\n"
                b"-7.69697,34.59667,307.24,0.52,0.67,2026-09-23,0001,N,nominal,2.0NRT,286.27,1.71,N\n"
                b"bad,row\n")
        rows = list(firms.parse("N", blob))
        self.assertEqual(rows, [("N", -7.69697, 34.59667, datetime(2026, 9, 23, 0, 1, tzinfo=timezone.utc), 1.71, "nominal", "N", 307.24)])
