---
name: volcano
description: Access USGS volcano alert status (colour code and alert level for US volcanoes, including Cascades, Alaska, Hawaii and the Marianas) directly or through news-radar's stored copy. Use for anything about which volcanoes are elevated, when a volcano's colour code changed, the latest observatory notice, or the volcano layer on the news-radar map. Covers the VSC and HANS endpoints, why a return to Green is invisible in the feed, and working SQL.
---

# USGS volcanoes

Adapter: `newsradar/adapters/volcano.py`. Spec: `specs/ingestion-expansion.md` R26.

## The source

- Feed: `https://volcanoes.usgs.gov/vsc/api/volcanoApi/elevated`. No key,
  public domain. JSON list, one object per volcano above Normal/Green:
  `vName`, `vnum` (Smithsonian number, stable), `lat`, `long`,
  `colorCode` and `alertLevel` with their `...Prev` values,
  `codeChangeDate` (UTC, when the current code began), `nvewsThreat`,
  `obs` (observatory), `noticeSynopsis`, `noticeUrl`. Four volcanoes on
  2026-09-24: Kilauea and Great Sitkin ORANGE, Shishaldin and Ahyi YELLOW.
- Related HANS endpoints, not used by the adapter:
  `/hans-public/api/volcano/getElevatedVolcanoes` (same list, no points)
  and `/hans-public/api/notice/getNewestOrRecent` (every recent notice,
  about 5 a day, with per-volcano sections).
- Codes: aviation colour GREEN, YELLOW, ORANGE, RED; ground alert level
  NORMAL, ADVISORY, WATCH, WARNING. `UNASSIGNED` means not monitored well
  enough to assign one.

## Traps

- **A return to Green is invisible.** The volcano simply leaves the list.
  The stored row keeps its last state, and `props.seen` (the fetch time)
  goes stale, which is how `primary_live` drops it. For the moment it went
  Green, read the HANS notices.
- Timestamps are `YYYY-MM-DD HH:MM:SS` in UTC with no zone marker.
- `codeChangeDate` can be years old (Great Sitkin: 2021) while notices are
  daily; `added_at` is the change, `props.notice_sent` the latest notice.

## The stored copy

`event` rows with source `usgs-volcanoes`, `attention = false`, keyed
`<vnum>:<codeChangeDate>`, so each colour change is its own row. `props`:
`color`, `alert`, `color_prev`, `alert_prev`, `threat`, `observatory`,
`synopsis`, `notice_id`, `notice_sent`, `seen`, `title`.

```sql
-- Colour history for one volcano, newest first.
SELECT added_at AS changed, props->>'color_prev' AS was, props->>'color' AS became,
       props->>'notice_sent' AS latest_notice
FROM event e JOIN source s ON s.id = e.source_id AND s.name = 'usgs-volcanoes'
WHERE geo_name = 'Kilauea' ORDER BY added_at DESC;

-- Elevated right now.
SELECT geo_name, props->>'color', props->>'synopsis' FROM primary_live WHERE source = 'volcano';
```

Fetched every 15 min by `ingest.py primary`.
