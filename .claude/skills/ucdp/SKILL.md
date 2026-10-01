---
name: ucdp
description: Access UCDP candidate events (researcher-coded organised violence with fatality estimates, worldwide, about a month behind) directly or through news-radar's stored copy. Use for anything about who fought whom, how many died, state-based versus non-state versus one-sided violence, conflict events by country or date, or the conflict layer on the news-radar map. Covers why the API is not used, the two release shapes on the downloads page, revisions, coding status, location precision, and working SQL.
---

# UCDP candidate events

Adapter: `newsradar/adapters/ucdp.py`. Spec: `specs/conflict-sources.md` R35.

## The source

- Downloads page: `https://ucdp.uu.se/downloads/`. Candidate CSVs live
  under `candidateged/`. CC BY 4.0, no key.
- **The API needs a token.** `ucdpapi.pcr.uu.se/api/gedevents/...` answers
  401 "API token required" without an `x-ucdp-access-token` header
  (2026-09-30). The CSVs carry the same rows.
- Two release shapes, both on the page at once:
  - monthly, `GEDEvent_v26_0_8.csv` = August 2026, 1,806 events, 1.4 MB;
  - consolidated, `GEDEvent_v26_01_26_06.csv` = January to June,
    10,051 events, 7.8 MB.
  The final yearly GED (`ged/ged261-csv.zip`, 1989 to 2025) and the VPP
  files are other datasets; the adapter's link regex excludes them.
- Lag: the August file was on the page by 2026-09-30.
- Columns that matter: `id` (stable across releases), `type_of_violence`
  (1 state-based, 2 non-state, 3 one-sided against civilians),
  `conflict_name`, `dyad_name`, `side_a`, `side_b`, `best`/`low`/`high`
  deaths, `deaths_civilians`, `where_prec` (1 exact point to 7 country
  centroid), `date_prec`, `date_start`, `date_end`, `code_status`,
  `source_office`, `source_headline`.

## Traps

- **Ids recur across releases.** A later file revises an earlier event
  (26 ids recurred in the first 2026 backfill). Load oldest first;
  `candidate_files()` sorts by release.
- **`code_status` is not `Clear` for about 43% of a monthly file**
  ("Check dyad", "Check deaths", "Check geography"...). These are
  provisional, not wrong; they are stored and shown.
- **`where_prec` 6 and 7 are country-level centroids**, not places. Do not
  read a cluster of them as fighting at one spot.
- **No per-event publish time.** `added_at` is when we first fetched a file
  carrying the event and is kept on revision, so a backfilled release lands
  at one timestamp. Use `occurred_on` (= `date_start`) for anything about
  when violence happened.
- `best = 0` rows exist (about 11% of August): coded events with no
  confirmed deaths.

## The stored copy

`event` rows with source `ucdp-candidate`, `attention = false`, keyed by
UCDP `id`, reverse geocoded to FIPS `country`. `actor1_name`/`actor2_name`
are `side_a`/`side_b`, `num_sources` is `number_of_sources`; everything
else is in `props` under the column names above. First load
(2026-09-30): 13,659 events dated 2026-01-01 to 2026-08-31.
`primary_live` shows the last 60 days by event date.

```sql
-- Deaths by country and type of violence, last 90 days of event dates.
SELECT country, props->>'type_of_violence' AS tov, count(*) AS events,
       sum((props->>'best')::int) AS deaths
FROM event e JOIN source s ON s.id = e.source_id AND s.name = 'ucdp-candidate'
WHERE occurred_on > current_date - 90
GROUP BY 1, 2 ORDER BY deaths DESC LIMIT 20;

-- Which releases have been loaded.
SELECT file, rows_kept, fetched_at FROM fetch_log f
JOIN source s ON s.id = f.source_id AND s.name = 'ucdp-candidate'
WHERE file NOT LIKE 'poll:%' ORDER BY fetched_at;
```

Polled daily by `ingest.py slow` (`news-radar-slow.timer`); a day with no
new file logs a `poll:` row with zero rows.
