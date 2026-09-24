---
name: usgs
description: Access USGS earthquake data directly or through news-radar's stored copy. Use for anything about recent earthquakes, their magnitude, depth, location or tsunami flag, or the quake layer on the news-radar map. Covers the GeoJSON summary feed, why events must be upserted, the offshore empty-country trap, and working SQL.
---

# USGS earthquakes

Adapter: `newsradar/adapters/usgs.py`. Spec: `specs/news-radar.md` R17.

## The source

- Feed: `https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson`
  (M4.5+ over the past day). Sibling feeds swap the magnitude
  (`significant`, `2.5`, `1.0`, `all`) and window (`hour`, `day`, `week`,
  `month`). No auth, public domain, refreshed every minute.
- GeoJSON FeatureCollection. `id` is stable. `properties.time` is when the
  quake happened and `properties.updated` the last revision, both epoch
  milliseconds. Coordinates are `[lon, lat, depth_km]`.
- **Events are revised for hours**: magnitude, depth and `alert` (PAGER
  level) change in place, so the adapter upserts on `id`.
- Volume: about 10 to 15 M4.5+ quakes a day.

## The stored copy

`event` rows with source `usgs-quakes`, `attention = false` (never counted
as coverage). Fields GDELT has no column for are in `props`: `mag`,
`depth_km`, `alert`, `tsunami`, `sig`, `updated`, `title`.

- `added_at` is the quake time, not when we fetched it.
- `country` is reverse geocoded against Natural Earth: containing polygon,
  else nearest within 100 km, else `''`. **Mid-ocean quakes (Tonga, the
  Marianas) stay `''`**; use `geo_name` ("180 km NW of Hihifo, Tonga").

```sql
SELECT to_char(added_at, 'YYYY-MM-DD HH24:MI') AS t, props->>'mag' AS mag,
       geo_name, country
FROM event e JOIN source s ON s.id = e.source_id AND s.name = 'usgs-quakes'
WHERE added_at > now() - interval '1 day'
ORDER BY (props->>'mag')::numeric DESC LIMIT 10;
```

Fetched every 15 min by `ingest.py primary` inside
`news-radar-ingest.service`.
