---
name: ioda
description: Access IODA internet outage detection (Georgia Tech) directly or through news-radar's stored copy. Use for anything about internet outages or shutdowns by country or region, connectivity blackouts that may signal power failures, conflict or government shutdowns, or the outage layer on the news-radar map. Covers the v2 API, the all-rights-reserved licence, why the query window must stay short, ISO versus FIPS codes, and working SQL.
---

# IODA internet outages

Adapter: `newsradar/adapters/ioda.py`. Spec: `specs/ingestion-expansion.md` R28.

## The source

- Base: `https://api.ioda.inetintel.cc.gatech.edu/v2`, no key.
  - `/outages/events?from=<epoch>&until=<epoch>&limit=5000`: detected
    outages, each `{location: "country/TN" | "region/3332" | "asn/...",
    location_name, start (epoch s), duration (s), datasource, score, method}`.
  - `/outages/alerts?...`: the raw per-signal alerts events are built from.
  - `/entities/query?entityType=region&entityCode=<code>`: a region's
    name, `attrs.country_code` (ISO2) and `attrs.ne_region_id`.
- Datasources: `bgp` (routed address space), `ping-slash24` (active
  probing), `merit-nt` (network telescope), `gtr` (Google traffic).
- **Licence: "Copyright Georgia Tech Research Corporation. All Rights
  Reserved"**, in every response. Personal use only; never republish.

## Traps

- **Keep the window short.** A 7-day `from` came back truncated and
  weighted to old starts: nothing from the last 48 h (measured
  2026-09-24). Every outage overlapping the window is returned, so a 6 h
  window re-read every 15 min misses nothing and long outages still grow.
- **No ids.** An outage is (datasource, entity, start); its `duration`
  grows in place while it lasts.
- **ISO codes, not FIPS.** `country/LT` is Lithuania, GDELT's `LH`. The
  store maps through `country_code` (GeoNames), loaded by
  `scripts/load_country_codes.py`.
- ASN and geo-ASN rows are most of the volume and name networks, not
  places; only country and region are stored.
- Region outages lasting days are common (monitoring gaps in sparse
  networks). The map shows only outages that started in the last 48 h.
- No points. The store places an outage at its country's point; a
  region's own polygon would need Natural Earth admin-1 via `ne_region_id`.

## The stored copy

`event` rows with source `ioda-outages`, `attention = false`, keyed
`<datasource>:<entity>:<start>`. `country` is FIPS. `props`:
`entity_type`, `entity_code`, `region_name`, `datasource`, `duration_s`,
`end`, `score`, `method`. `ioda_region` caches region to country.

```sql
-- National outages in the last week, longest first.
SELECT geo_name, country, props->>'datasource' AS signal, added_at AS start,
       round((props->>'duration_s')::numeric / 3600, 1) AS hours
FROM event e JOIN source s ON s.id = e.source_id AND s.name = 'ioda-outages'
WHERE props->>'entity_type' = 'country' AND added_at > now() - interval '7 days'
ORDER BY hours DESC;
```

Fetched every 15 min by `ingest.py primary`.
