"""NASA FIRMS active fire detections (R27), VIIRS on the three polar
satellites, as the public 24 h global CSVs. No key for these files; NASA
open data, citation requested.

A row is a 375 m pixel a satellite saw burning, not a fire, and there is no
id. So detections go to `firms_detection` keyed (satellite, lat, lon, time),
and `load` groups them into fires incrementally: a new detection within 1 km
of a fire active in the last 48 h joins it; the rest cluster among themselves
(DBSCAN, eps ~1 km) into new fires. Each fire is an `event` row, keyed on
its first detection, with its extent and running totals in `fire`.

Each file is a rolling 24 h window refreshed per satellite pass. A file is
downloaded only when its Last-Modified changes, recorded in fetch_log."""
from __future__ import annotations

import csv
import io
import urllib.request
from datetime import datetime, timezone
from typing import Iterator

KIND = "firms"
SOURCE = "firms-fires"
BASE = "https://firms.modaps.eosdis.nasa.gov/data/active_fire/"
FILES = {
    "N": "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_24h.csv",
    "N20": "noaa-20-viirs-c2/csv/J1_VIIRS_C2_Global_24h.csv",
    "N21": "noaa-21-viirs-c2/csv/J2_VIIRS_C2_Global_24h.csv",
}
EPS_DEG = 0.009      # ~1 km of latitude; DBSCAN runs in degrees
JOIN_M = 1000        # a detection this close to a live fire's hull joins it
ACTIVE_HOURS = 48    # a fire with no detection for this long is closed


def _req(url: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(url, method=method, headers={"User-Agent": "news-radar/0.1"})


def last_modified(sat: str) -> str:
    with urllib.request.urlopen(_req(BASE + FILES[sat], "HEAD"), timeout=30) as r:
        return r.headers.get("Last-Modified", "")


def fetch(sat: str) -> bytes:
    with urllib.request.urlopen(_req(BASE + FILES[sat]), timeout=120) as r:
        return r.read()


def parse(sat: str, blob: bytes) -> Iterator[tuple]:
    """(satellite, lat, lon, acq_at, frp, confidence, daynight, bright_ti4)."""
    for r in csv.DictReader(io.StringIO(blob.decode("utf-8", "replace"))):
        try:
            t = r["acq_time"].zfill(4)
            at = datetime.strptime(f"{r['acq_date']} {t}", "%Y-%m-%d %H%M").replace(tzinfo=timezone.utc)
            yield (sat, float(r["latitude"]), float(r["longitude"]), at,
                   float(r["frp"]) if r.get("frp") else None, r.get("confidence"), r.get("daynight"),
                   float(r["bright_ti4"]) if r.get("bright_ti4") else None)
        except (KeyError, ValueError, TypeError, AttributeError):
            continue  # truncated or malformed row


def insert(conn, rows: Iterator[tuple]) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE fstaging (satellite text, lat float8, lon float8, acq_at timestamptz, "
                    "frp real, confidence text, daynight text, bright_ti4 real) ON COMMIT DROP")
        n = 0
        with cur.copy("COPY fstaging FROM STDIN") as copy:
            for r in rows:
                copy.write_row(r)
                n += 1
        cur.execute("""
            INSERT INTO firms_detection (satellite, lat, lon, acq_at, frp, confidence, daynight, bright_ti4, geom)
            SELECT satellite, lat, lon, acq_at, frp, confidence, daynight, bright_ti4,
                   ST_SetSRID(ST_MakePoint(lon, lat), 4326)
            FROM fstaging
            ON CONFLICT (satellite, lat, lon, acq_at) DO NOTHING""")
        return n, cur.rowcount


def cluster(conn, sid: int) -> dict[str, int]:
    """Assign unassigned detections to fires, then refresh fire totals and the
    fire's event row. Runs in the caller's transaction."""
    out = {}
    # 1. Join live fires: nearest live fire within JOIN_M of the hull.
    out["joined"] = conn.execute("""
        WITH m AS (
            SELECT d.id, (SELECT f.event_id FROM fire f
                          WHERE f.last_seen > d.acq_at - make_interval(hours => %(active)s)
                            AND ST_DWithin(f.hull, d.geom, %(eps)s * 2)
                            AND ST_DWithin(f.hull::geography, d.geom::geography, %(join)s)
                          ORDER BY f.hull <-> d.geom LIMIT 1) AS fid
            FROM firms_detection d WHERE d.fire_id IS NULL
        )
        UPDATE firms_detection d SET fire_id = m.fid FROM m
        WHERE d.id = m.id AND m.fid IS NOT NULL""", {"active": ACTIVE_HOURS, "eps": EPS_DEG, "join": JOIN_M}).rowcount
    # 2. The rest cluster among themselves; each cluster is a new fire.
    conn.execute("""
        CREATE TEMP TABLE newfire ON COMMIT DROP AS
        SELECT d.id, ST_ClusterDBSCAN(d.geom, eps := %s, minpoints := 1) OVER () AS c
        FROM firms_detection d WHERE d.fire_id IS NULL""", (EPS_DEG,))
    rows = conn.execute("""
        WITH first AS (
            SELECT DISTINCT ON (n.c) n.c, d.id, d.acq_at, d.lat, d.lon
            FROM newfire n JOIN firms_detection d ON d.id = n.id
            ORDER BY n.c, d.acq_at, d.id
        )
        INSERT INTO event (source_id, external_id, occurred_on, added_at, country, geom,
                           num_mentions, num_sources, num_articles, props)
        SELECT %s, 'firms:' || id, (acq_at AT TIME ZONE 'UTC')::date, acq_at, '',
               ST_SetSRID(ST_MakePoint(lon, lat), 4326), 1, 1, 1, '{"kind": "fire"}'::jsonb
        FROM first
        ON CONFLICT (source_id, external_id) DO NOTHING
        RETURNING id, external_id""", (sid,)).fetchall()
    out["new_fires"] = len(rows)
    conn.execute("""
        UPDATE firms_detection d SET fire_id = e.id
        FROM newfire n
        JOIN (SELECT DISTINCT ON (n2.c) n2.c, d2.id AS first_id FROM newfire n2 JOIN firms_detection d2 ON d2.id = n2.id
              ORDER BY n2.c, d2.acq_at, d2.id) f ON f.c = n.c
        JOIN event e ON e.source_id = %s AND e.external_id = 'firms:' || f.first_id
        WHERE d.id = n.id""", (sid,))
    # 3. Refresh totals for every fire touched in the active window.
    conn.execute("""
        INSERT INTO fire (event_id, hull, detections, high_conf, frp_sum, frp_max, first_seen, last_seen)
        SELECT fire_id, ST_ConvexHull(ST_Collect(geom)), count(*), count(*) FILTER (WHERE confidence IN ('high', 'h')),
               coalesce(sum(frp), 0), coalesce(max(frp), 0), min(acq_at), max(acq_at)
        FROM firms_detection
        WHERE fire_id IN (SELECT DISTINCT fire_id FROM firms_detection
                          WHERE acq_at > now() - make_interval(hours => %s) AND fire_id IS NOT NULL)
        GROUP BY fire_id
        ON CONFLICT (event_id) DO UPDATE SET hull = EXCLUDED.hull, detections = EXCLUDED.detections,
            high_conf = EXCLUDED.high_conf, frp_sum = EXCLUDED.frp_sum, frp_max = EXCLUDED.frp_max,
            first_seen = EXCLUDED.first_seen, last_seen = EXCLUDED.last_seen""", (ACTIVE_HOURS * 2,))
    out["refreshed"] = conn.execute("""
        UPDATE event e SET geom = ST_PointOnSurface(f.hull),
               props = jsonb_build_object('kind', 'fire', 'title', 'Active fire',
                   'detections', f.detections, 'high_conf', f.high_conf,
                   'frp_sum', round(f.frp_sum::numeric, 1), 'frp_max', round(f.frp_max::numeric, 1),
                   'first_seen', f.first_seen, 'last_seen', f.last_seen,
                   'area_km2', round((ST_Area(f.hull::geography) / 1e6)::numeric, 2))
        FROM fire f
        WHERE f.event_id = e.id AND f.last_seen > now() - make_interval(hours => %s)""", (ACTIVE_HOURS * 2,)).rowcount
    return out


def load(conn, sid: int, log_fetch) -> str:
    """Fetch each satellite's file if it changed, insert, cluster, all in the
    caller's transaction. log_fetch is store.log_fetch."""
    seen = kept = files = 0
    for sat in FILES:
        lm = last_modified(sat)
        key = f"{sat}:{lm}"
        if conn.execute("SELECT 1 FROM fetch_log WHERE source_id = %s AND file = %s", (sid, key)).fetchone():
            continue
        n, k = insert(conn, parse(sat, fetch(sat)))
        conn.commit()  # fstaging is ON COMMIT DROP; one per file (see CLAUDE.md)
        log_fetch(conn, sid, key, n, k)
        seen, kept, files = seen + n, kept + k, files + 1
    c = cluster(conn, sid) if files else {}
    return f"{files} files, {seen} rows, {kept} new detections, {c}"
