#!/usr/bin/env python3
"""Derive the attention series from event and the mention buffer, then the
anomaly table.

events counts first sightings (from event). mentions and sources count
coverage in that hour (from the raw mention buffer, R19), so a story that
keeps running keeps its region lit. They are only recomputed for hours the
buffer still covers; older rows keep what was computed while it did.

    rollup.py            # last 48 h of hourly and daily, anomaly for last 48 h, archive + prune
    rollup.py --full     # every hour in the table (after a backfill)

Rollups upsert, so re-running a window is safe. Anomaly compares each hour to
the same hour of day over the trailing 30 days with missing rows as zero and a
Poisson floor on the standard deviation; see schema.sql.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from newsradar import archive
from newsradar.db import connect, load_env

# Hot windows (specs/archive.md, Decision 5). Every table here is archived to
# Parquet under NEWSRADAR_ARCHIVE_DIR before its rows are deleted; the window
# is only how long a row stays in Postgres.
ARCHIVE_WINDOWS = {
    "event": 60,              # primary_live's UCDP 60-day event-date clause
    "theme_hourly": 7,        # nothing reads it
    "mention_hourly": 7,      # nothing reads it
    "attention_hourly": 35,   # _ANOMALY looks 30 days back
    "wiki_activity": 30,
    "chatter_hourly": 30,
    "gkg_article": 30,        # with its gkg_location rows
    "firms_detection": 30,
    "theme_daily": 180,
}
# Scratch buffers: deleted without an archive.
RETAIN_ANOMALY_DAYS = 7
RETAIN_MENTION_HOURS = 72
RETAIN_CHATTER_5MIN_DAYS = 7
RETAIN_BSKY_URI_HOURS = 48

_HOURLY_EVENTS = """
INSERT INTO attention_hourly (region_kind, region, hour, events, mentions, sources)
SELECT %(kind)s, {region}, date_trunc('hour', added_at), count(*), 0, 0
FROM event
WHERE added_at >= %(since)s AND {region} IS NOT NULL AND {region} <> ''
  AND source_id IN (SELECT id FROM source WHERE attention)
GROUP BY 2, 3
ON CONFLICT (region_kind, region, hour) DO UPDATE SET events = EXCLUDED.events
"""

_HOURLY_MENTIONS = """
INSERT INTO attention_hourly (region_kind, region, hour, events, mentions, sources)
SELECT %(kind)s, e.{region}, date_trunc('hour', m.mentioned_at), 0, count(*), count(DISTINCT m.source_name)
FROM mention m JOIN event e ON e.id = m.event_id
WHERE m.mentioned_at >= %(since)s AND e.{region} IS NOT NULL AND e.{region} <> ''
GROUP BY 2, 3
ON CONFLICT (region_kind, region, hour) DO UPDATE
SET mentions = EXCLUDED.mentions, sources = EXCLUDED.sources
"""

_WIKI_ACTIVITY = """
INSERT INTO wiki_activity (wiki, title, hour, edits, editors, created)
SELECT wiki, title, date_trunc('hour', at), count(*), count(DISTINCT user_hash) FILTER (WHERE NOT bot), bool_or(created)
FROM wiki_edit
WHERE at >= %(since)s
GROUP BY 1, 2, 3
ON CONFLICT (wiki, title, hour) DO UPDATE
SET edits = EXCLUDED.edits, editors = EXCLUDED.editors, created = EXCLUDED.created
"""

# chatter_5min is written additively by the stream consumer; hourly is a
# plain recompute from it, so re-running is safe.
_CHATTER_HOURLY = """
INSERT INTO chatter_hourly (source, place_id, hour, posts)
SELECT source, place_id, date_trunc('hour', bucket), sum(posts)
FROM chatter_5min
WHERE bucket >= %(since)s
GROUP BY 1, 2, 3
ON CONFLICT (source, place_id, hour) DO UPDATE SET posts = EXCLUDED.posts
"""

_MENTION_HOURLY = """
INSERT INTO mention_hourly (event_id, hour, mentions, sources)
SELECT event_id, date_trunc('hour', mentioned_at), count(*), count(DISTINCT source_name)
FROM mention
WHERE mentioned_at >= %(since)s
GROUP BY 1, 2
ON CONFLICT (event_id, hour) DO UPDATE SET mentions = EXCLUDED.mentions, sources = EXCLUDED.sources
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
    # Whole hours only: a window starting mid-hour would re-upsert that hour
    # from a partial count. Same for the buffer's first hour after a prune.
    since = since.replace(minute=0, second=0, microsecond=0)
    first = conn.execute("""SELECT CASE WHEN min(mentioned_at) = date_trunc('hour', min(mentioned_at))
                                        THEN min(mentioned_at)
                                        ELSE date_trunc('hour', min(mentioned_at)) + interval '1 hour' END
                            FROM mention""").fetchone()[0]
    msince = max(since, first) if first else None
    with conn.transaction():
        for kind, region in (("country", "country"), ("adm1", "adm1")):
            out[f"hourly_{kind}"] = conn.execute(_HOURLY_EVENTS.format(region=region),
                                                 {"kind": kind, "since": since}).rowcount
            if msince:
                conn.execute(_HOURLY_MENTIONS.format(region=region), {"kind": kind, "since": msince})
        if msince:
            out["mention_hourly"] = conn.execute(_MENTION_HOURLY, {"since": msince}).rowcount
        # The wiki buffer is 72 h; roll only whole hours it still covers.
        wfirst = conn.execute("""SELECT CASE WHEN min(at) = date_trunc('hour', min(at)) THEN min(at)
                                             ELSE date_trunc('hour', min(at)) + interval '1 hour' END
                                 FROM wiki_edit""").fetchone()[0]
        if wfirst:
            out["wiki_activity"] = conn.execute(_WIKI_ACTIVITY, {"since": max(since, wfirst)}).rowcount
        out["chatter_hourly"] = conn.execute(_CHATTER_HOURLY, {"since": since}).rowcount
        out["daily"] = conn.execute(_DAILY, {"since": since}).rowcount
        if anomaly:
            out["anomaly"] = conn.execute(_ANOMALY, {"since": since, "until": until}).rowcount
            out["names"] = conn.execute(_NAMES).rowcount
    return out


def prune_buffers(conn, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    with conn.transaction():
        return {
            "mention": conn.execute("DELETE FROM mention WHERE mentioned_at < %s",
                                    (now - timedelta(hours=RETAIN_MENTION_HOURS),)).rowcount,
            "wiki_edit": conn.execute("DELETE FROM wiki_edit WHERE at < %s",
                                      (now - timedelta(hours=RETAIN_MENTION_HOURS),)).rowcount,
            "chatter_5min": conn.execute("DELETE FROM chatter_5min WHERE bucket < %s",
                                         (now - timedelta(days=RETAIN_CHATTER_5MIN_DAYS),)).rowcount,
            "bsky_uri": conn.execute("DELETE FROM bsky_uri WHERE at < %s",
                                     (now - timedelta(hours=RETAIN_BSKY_URI_HOURS),)).rowcount,
            "anomaly": conn.execute("DELETE FROM attention_anomaly WHERE hour < %s",
                                    (now - timedelta(days=RETAIN_ANOMALY_DAYS),)).rowcount,
        }


_ENV = object()


def prune(conn, archive_dir=_ENV, scratch_dir=_ENV, now: datetime | None = None) -> dict:
    """Scratch buffers are deleted outright. Archived tables are written to
    Parquet first and only deleted once the file is verified (specs/archive.md);
    with no archive dir configured they are left alone (R62), never deleted
    unarchived. Failed tables are listed under "failed"."""
    env = load_env() if _ENV in (archive_dir, scratch_dir) else {}
    if archive_dir is _ENV:
        archive_dir = env.get("NEWSRADAR_ARCHIVE_DIR") or None
    if scratch_dir is _ENV:
        scratch_dir = env.get("NEWSRADAR_ARCHIVE_SCRATCH") or None
    conn.commit()
    out: dict = prune_buffers(conn, now)
    if not archive_dir:
        out["archive"] = "skipped: NEWSRADAR_ARCHIVE_DIR unset"
        return out
    try:
        done, failed = archive.prune_archived(conn, ARCHIVE_WINDOWS, archive_dir, scratch_dir, now)
    except archive.ArchiveError as e:
        done, failed = {}, {"archive": str(e)}
    out.update(done)
    if failed:
        out["failed"] = failed
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="roll up every hour present, not just the last 48")
    ap.add_argument("--no-prune", action="store_true")
    args = ap.parse_args(argv)
    with connect() as conn:
        if args.full:
            row = conn.execute("SELECT min(added_at) FROM event").fetchone()
            since = row[0] or datetime.now(timezone.utc)
            # event outlives attention_hourly's window; rebuilding hours past it
            # would resurrect rows already archived, with events but no mentions.
            floor = datetime.now(timezone.utc) - timedelta(days=ARCHIVE_WINDOWS["attention_hourly"])
            since = max(since, floor.replace(hour=0, minute=0, second=0, microsecond=0))
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
    if counts.get("pruned", {}).get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
