---
name: gdacs
description: Access GDACS global disaster alerts (earthquake, tropical cyclone, flood, volcano, drought, wildfire, tsunami) directly or through news-radar's stored copy. Use for anything about current disasters worldwide, their alert level, or the GDACS layer on the news-radar map. Covers the RSS feed and its georss points, the iscurrent trap, future-dated forecast floods, and working SQL.
---

# GDACS alerts

Adapter: `newsradar/adapters/gdacs.py`. Spec: `specs/news-radar.md` R17.

## The source

- Feed: `https://www.gdacs.org/xml/rss.xml`. No auth. GDACS is run by the
  UN and the EU Commission's JRC.
- RSS items with `gdacs:` and `georss:` namespaces. One item per event
  episode. Stable key is `eventtype` + `eventid` (e.g. `TC1001234`).
- `alertlevel` moves Green, Orange, Red as an event develops, and severity
  and population are revised, so the adapter upserts.
- Types: EQ, TC, FL, VO, DR, WF, TS. Tropical cyclones are global here,
  which is why NHC was dropped.
- About 390 items in the feed at a time, most of them Green.

## Traps

- **`iscurrent` is false on live Orange droughts** (all six, measured
  2026-09-20). Filter by `datemodified`, never by that flag.
- **`fromdate` can be in the future** for forecast floods. It is stored as
  `occurred_on`; `dateadded` is `added_at`.
- The point is a representative location. A multi-country drought gets one
  point and one geocoded country; the item's `iso3` and title list more.

## The stored copy

`event` rows with source `gdacs-alerts`, `attention = false`. `props`
carries `kind`, `event_type`, `alert`, `alert_score`, `episode`, `iso3`,
`severity`, `severity_value`, `population`, `current`, `modified`, `title`.

```sql
-- Non-Green alerts touched in the last week.
SELECT props->>'kind' AS kind, props->>'alert' AS alert,
       props->>'title' AS title, country
FROM event e JOIN source s ON s.id = e.source_id AND s.name = 'gdacs-alerts'
WHERE props->>'alert' <> 'Green'
  AND (props->>'modified')::timestamptz > now() - interval '7 days'
ORDER BY (props->>'modified')::timestamptz DESC;
```

Fetched every 15 min by `ingest.py primary` inside
`news-radar-ingest.service`.
