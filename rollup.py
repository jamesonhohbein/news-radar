#!/usr/bin/env python3
"""Derive the attention series from event, then the anomaly table.

    rollup.py            # last 48 h of hourly and daily, anomaly for last 48 h, prune
    rollup.py --full     # every hour in the table (after a backfill)

Rollups upsert, so re-running a window is safe. Anomaly compares each hour to
the same hour of day over the trailing 30 days with missing rows as zero and a
Poisson floor on the standard deviation; see schema.sql.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from newsradar.db import connect

RETAIN_EVENT_DAYS = 90
RETAIN_HOURLY_DAYS = 90
RETAIN_ANOMALY_DAYS = 7

_HOURLY = """
INSERT INTO attention_hourly (region_kind, region, hour, events, mentions, sources)
SELECT %(kind)s, {region}, date_trunc('hour', added_at), count(*), sum(num_mentions), sum(num_sources)
FROM event
WHERE added_at >= %(since)s AND {region} IS NOT NULL AND {region} <> ''
GROUP BY 2, 3
ON CONFLICT (region_kind, region, hour) DO UPDATE
SET events = EXCLUDED.events, mentions = EXCLUDED.mentions, sources = EXCLUDED.sources
"""

_DAILY = """
INSERT INTO attention_daily (region_kind, region, day, events, mentions, sources)
SELECT region_kind, region, (hour AT TIME ZONE 'UTC')::date, sum(events), sum(mentions), sum(sources)
FROM attention_hourly
WHERE hour >= date_trunc('day', %(since)s::timestamptz)
GROUP BY 1, 2, 3
ON CONFLICT (region_kind, region, day) DO UPDATE
SET events = EXCLUDED.events, mentions = EXCLUDED.mentions, sources = EXCLUDED.sources
"""

# For every (region, hour) with activity in the window, the 30 same-hour rows
# before it, absent rows as zero. sd floor: greatest(sd, sqrt(mean), 1).
_ANOMALY = """
WITH cand AS (
    SELECT region_kind, region, hour, mentions
    FROM attention_hourly
    WHERE hour >= %(since)s AND hour < %(until)s
),
base AS (
    SELECT c.region_kind, c.region, c.hour, c.mentions,
           avg(coalesce(h.mentions, 0))        AS mean,
           stddev_pop(coalesce(h.mentions, 0)) AS sd
    FROM cand c
    CROSS JOIN generate_series(1, 30) AS d
    LEFT JOIN attention_hourly h
           ON h.region_kind = c.region_kind AND h.region = c.region
          AND h.hour = c.hour - d * interval '1 day'
    GROUP BY 1, 2, 3, 4
)
INSERT INTO attention_anomaly (region_kind, region, hour, mentions, baseline_mean, baseline_sd, z, computed_at)
SELECT region_kind, region, hour, mentions, mean, sd,
       (mentions - mean) / greatest(sd, sqrt(greatest(mean, 0)), 1),
       now()
FROM base
ON CONFLICT (region_kind, region, hour) DO UPDATE
SET mentions = EXCLUDED.mentions, baseline_mean = EXCLUDED.baseline_mean,
    baseline_sd = EXCLUDED.baseline_sd, z = EXCLUDED.z, computed_at = now()
"""


# geo_type 1 is "country" in GDELT's coding, so geo_name there is the
# country's own name; mode() picks the spelling the source uses most.
_NAMES = """
INSERT INTO region_name (region_kind, region, name)
SELECT 'country', country, mode() WITHIN GROUP (ORDER BY geo_name)
FROM event
WHERE geo_type = 1 AND geo_name IS NOT NULL AND added_at > now() - interval '7 days'
GROUP BY country
ON CONFLICT (region_kind, region) DO UPDATE SET name = EXCLUDED.name
"""


def rollup(conn, since: datetime, until: datetime | None = None, anomaly: bool = True) -> dict[str, int]:
    until = until or datetime.now(timezone.utc) + timedelta(hours=1)
    out = {}
    with conn.transaction():
        for kind, region in (("country", "country"), ("adm1", "adm1")):
            out[f"hourly_{kind}"] = conn.execute(_HOURLY.format(region=region),
                                                 {"kind": kind, "since": since}).rowcount
        out["daily"] = conn.execute(_DAILY, {"since": since}).rowcount
        if anomaly:
            out["anomaly"] = conn.execute(_ANOMALY, {"since": since, "until": until}).rowcount
            out["names"] = conn.execute(_NAMES).rowcount
    return out


def prune(conn) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    with conn.transaction():
        return {
            "event": conn.execute("DELETE FROM event WHERE added_at < %s",
                                  (now - timedelta(days=RETAIN_EVENT_DAYS),)).rowcount,
            "hourly": conn.execute("DELETE FROM attention_hourly WHERE hour < %s",
                                   (now - timedelta(days=RETAIN_HOURLY_DAYS),)).rowcount,
            "anomaly": conn.execute("DELETE FROM attention_anomaly WHERE hour < %s",
                                    (now - timedelta(days=RETAIN_ANOMALY_DAYS),)).rowcount,
        }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="roll up every hour present, not just the last 48")
    ap.add_argument("--no-prune", action="store_true")
    args = ap.parse_args()
    with connect() as conn:
        if args.full:
            row = conn.execute("SELECT min(added_at) FROM event").fetchone()
            since = row[0] or datetime.now(timezone.utc)
            # Anomaly needs 30 days of history under it, so it is only meaningful
            # for the last 48 h even on a full run.
            counts = rollup(conn, since, anomaly=False)
            recent = rollup(conn, datetime.now(timezone.utc) - timedelta(hours=48))
            counts["anomaly"] = recent["anomaly"]
        else:
            counts = rollup(conn, datetime.now(timezone.utc) - timedelta(hours=48))
        if not args.no_prune:
            counts["pruned"] = prune(conn)
        print(counts)


if __name__ == "__main__":
    main()
