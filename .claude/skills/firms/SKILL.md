---
name: firms
description: Access NASA FIRMS satellite fire detections (VIIRS on Suomi NPP, NOAA-20, NOAA-21) directly or through news-radar's stored copy. Use for anything about active wildfires, bushfires, peat or crop burning anywhere in the world, how large or intense a fire is, whether a fire is still burning, or the fire layer on the news-radar map. Covers the keyless 24 h CSVs, why a row is a pixel and not a fire, how detections are clustered into fires, and working SQL.
---

# NASA FIRMS fires

Adapter: `newsradar/adapters/firms.py`. Spec: `specs/ingestion-expansion.md` R27.

## The source

- Base: `https://firms.modaps.eosdis.nasa.gov/data/active_fire/`, three
  files, no key needed (NASA open data, citation requested):
  - `suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_24h.csv`
  - `noaa-20-viirs-c2/csv/J1_VIIRS_C2_Global_24h.csv`
  - `noaa-21-viirs-c2/csv/J2_VIIRS_C2_Global_24h.csv`
- Each is a rolling 24 h window, about 58k rows and 4.7 MB, rewritten
  after each satellite pass. Latency is roughly 3 h from overpass.
- Columns: `latitude, longitude, bright_ti4, scan, track, acq_date,
  acq_time (HHMM UTC), satellite, confidence (low|nominal|high), version,
  bright_ti5, frp (fire radiative power, MW), daynight`.
- Regional files (e.g. `USA_contiguous_and_Hawaii_24h`) and a keyed area
  API exist; the keyed API allows 5000 calls per 10 min.

## Traps

- **A row is a 375 m pixel, not a fire, and has no id.** Key it on
  (satellite, lat, lon, time). One fire is many pixels over many passes.
- The 24 h file overlaps the previous one almost entirely, so inserts are
  mostly conflicts. The adapter checks `Last-Modified` with a HEAD first
  and downloads only a changed file (keyed `<sat>:<Last-Modified>` in
  `fetch_log`).
- Around 47k clusters a day globally (2026-09-24), almost all small
  agricultural burns. Size, not presence, is the signal.

## How fires are formed

Each ingest: a new detection within 1 km of the hull of a fire seen in the
last 48 h joins it; the rest cluster among themselves (`ST_ClusterDBSCAN`,
eps 0.009 degrees, about 1 km) into new fires. A fire is an `event` row
(source `firms-fires`, keyed `firms:<first detection id>`), with extent and
running totals in `fire` and mirrored into `props`.

## The stored copy

| Table | Holds | Kept |
|---|---|---|
| `firms_detection` | every pixel, with `fire_id` = the fire's `event.id` | 30 d |
| `fire` | per fire: convex hull, detections, high-confidence count, FRP sum and max, first and last seen | |
| `event` (source `firms-fires`) | the fire; point on its hull, `props` mirror `fire` plus `area_km2` | 90 d |

```sql
-- Largest fires still burning.
SELECT country, props->>'detections' AS pixels, props->>'area_km2' AS km2,
       props->>'last_seen' AS last_seen, ST_Y(geom) AS lat, ST_X(geom) AS lon
FROM event e JOIN source s ON s.id = e.source_id AND s.name = 'firms-fires'
WHERE (props->>'last_seen')::timestamptz > now() - interval '24 hours'
ORDER BY (props->>'detections')::int DESC LIMIT 10;

-- Fires within 50 km of a point (Bellingham).
SELECT e.id, f.detections, f.last_seen FROM fire f JOIN event e ON e.id = f.event_id
WHERE ST_DWithin(f.hull::geography, ST_MakePoint(-122.48, 48.75)::geography, 50000)
ORDER BY f.last_seen DESC;
```

The map draws fires with 50+ detections burning in the last 24 h.
Checked every 15 min by `ingest.py primary`.
