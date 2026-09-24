---
name: radar
description: Access Cloudflare Radar outage annotations directly or through news-radar's stored copy. Use for anything about why an internet outage happened (power cut, cable cut, government shutdown, weather, cyberattack), nationwide versus regional outages, or the Radar entries on the news-radar outage layer. Covers the token it needs, the CC BY-NC licence, how to switch it on, and working SQL.
---

# Cloudflare Radar outages

Adapter: `newsradar/adapters/radar.py`. Spec: `specs/ingestion-expansion.md` R29.

## The source

- `https://api.cloudflare.com/client/v4/radar/annotations/outages`
  with `dateRange=7d&limit=500&format=json` and
  `Authorization: Bearer <token>`. Unauthenticated calls get 400 (code
  9106); `radar.cloudflare.com` itself sits behind a bot challenge.
- **Needs a token**: a free Cloudflare account, then a Custom API token
  with Account > Radar > Read.
- **Licence: CC BY-NC 4.0.** Fine for a personal instance; a deployment
  that republishes it inherits the non-commercial clause.
- Response: `result.annotations[]`, each with `id`, `startDate`,
  `endDate` (null while ongoing), `locations` (ISO2 list),
  `locationsDetails`, `asnsDetails`, `scope`, `description`, `linkedUrl`
  and `outage: {outageCause, outageType}`. Causes include POWER_OUTAGE,
  CABLE_CUT, GOVERNMENT_DIRECTED, WEATHER, CYBERATTACK (17 values); types
  are NATIONWIDE, REGIONAL, NETWORK, PLATFORM.
- **Not yet seen live.** The schema and fixture come from Cloudflare's API
  reference (2026-09-24); check the first real response against T27.

## Switching it on

1. Put `CLOUDFLARE_RADAR_TOKEN=<token>` in `.env` (paste it in an editor;
   see the machine-wide rule on secrets).
2. `docker compose exec -T db psql -U newsradar -d newsradar -c "UPDATE source SET enabled = true WHERE name = 'cloudflare-radar'"`

The source is seeded disabled so an instance without a token does not
show it as stale in `source_health`. With no token, `ingest.py primary`
logs `radar: skipped` and inserts nothing.

## The stored copy

`event` rows with source `cloudflare-radar`, `attention = false`, one per
annotation per location, keyed `<id>:<ISO2>`, placed at the country's
point. `props`: `cause`, `outage_type`, `scope`, `end`, `iso2`, `asns`,
`data_source`, `title` (the description).

```sql
SELECT added_at, geo_name, props->>'cause' AS cause, props->>'outage_type' AS type, props->>'end' AS ended
FROM event e JOIN source s ON s.id = e.source_id AND s.name = 'cloudflare-radar'
ORDER BY added_at DESC LIMIT 20;
```
