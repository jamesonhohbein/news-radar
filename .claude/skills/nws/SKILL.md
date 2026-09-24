---
name: nws
description: Access US National Weather Service alerts (warnings, watches, advisories) directly or through news-radar's stored copy. Use for anything about current or recent NWS alerts anywhere in the US, where an alert applies, whether it was updated or cancelled, or the NWS layer on the news-radar map. Covers the api.weather.gov endpoints and User-Agent requirement, why most alerts have no geometry and how zones fix it, the supersession chain, and working SQL.
---

# NWS alerts

Adapter: `newsradar/adapters/nws.py`. Spec: `specs/ingestion-expansion.md` R24.

## The source

- Feed: `https://api.weather.gov/alerts/active` with
  `Accept: application/geo+json`. No key, public domain.
- **A User-Agent is required.** An empty one gets 403. The adapter sends
  `news-radar/0.1 (github.com/jamesonhohbein/news-radar)`.
- GeoJSON FeatureCollection of CAP alerts. About 480 active at a time
  (measured 2026-09-24), 2.3 MB per poll. Most are marine: Small Craft
  Advisory and Gale Warning were 266 of 483.
- Other useful endpoints: `/alerts?area=WA` or `?point=48.75,-122.48`
  (history filter), `/alerts/{id}`, `/zones/{type}/{code}` (a zone's
  polygon).

## Traps

- **93% of alerts have `geometry: null`.** They name zones instead:
  `affectedZones` (URLs) and `geocode.UGC` (codes like `WAC073` for
  Whatcom County, `WAZ503` for a forecast zone). The adapter fetches each
  zone's polygon from `/zones/...` the first time it is referenced, caches
  it in `nws_zone`, and refetches after 90 days. The first run on an empty
  cache fetched about 630 zones.
- **Nothing is edited in place.** An Update or Cancel is a new alert id
  whose `references[]` name what it replaces. The store keeps both and
  sets `superseded_by` on the older row. "Current" means
  `superseded_by IS NULL` and `expires > now()`.
- `sent` is `added_at`; `onset` (else `effective`) is `occurred_on`. All
  timestamps carry the issuing office's UTC offset.
- US and territories only. Country comes from reverse geocoding the point.

## The stored copy

`event` rows with source `nws-alerts`, `attention = false`. The point is
`ST_PointOnSurface` of the alert's own polygon, or of the union of its
zones. `props` carries `event`, `severity`, `urgency`, `certainty`,
`message_type`, `expires`, `ends`, `sender`, `ugc`, `zones`, `references`,
`title`. `nws_zone(url, code, kind, name, geom)` holds the polygons, so an
alert's full area is a join on `url = ANY(props->'zones')`.

```sql
-- Current alerts for Whatcom County, most severe first.
SELECT props->>'event' AS event, props->>'severity' AS severity,
       props->>'expires' AS expires, geo_name
FROM event e JOIN source s ON s.id = e.source_id AND s.name = 'nws-alerts'
WHERE e.superseded_by IS NULL AND (props->>'expires')::timestamptz > now()
  AND props->'ugc' ? 'WAC073'
ORDER BY array_position(ARRAY['Extreme','Severe','Moderate','Minor','Unknown'], props->>'severity');

-- An alert's full area as one polygon.
SELECT ST_AsGeoJSON(ST_Union(z.geom))
FROM event e JOIN nws_zone z ON z.url IN (SELECT jsonb_array_elements_text(e.props->'zones'))
WHERE e.external_id = '<alert id>';
```

The map draws only the `primary_live` subset: unsuperseded, unexpired,
Severe or Extreme. Fetched every 15 min by `ingest.py primary` inside
`news-radar-ingest.service`.
