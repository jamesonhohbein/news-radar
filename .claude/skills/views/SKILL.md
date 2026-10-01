---
name: views
description: Access VIEWS conflict forecasts (Uppsala and PRIO's predicted state-based fatalities per country and month, 36 months ahead) directly or through news-radar's stored copy. Use for anything about where armed conflict is expected, forecast deaths for a country, how a forecast changed between monthly runs, or the forecast tint on the news-radar map. Covers the run list, the cm/sb endpoint and paging, what main_mean and main_dich mean, the ISO3-to-FIPS mapping, and working SQL.
---

# VIEWS forecasts

Adapter: `newsradar/adapters/views.py`. Spec: `specs/conflict-sources.md` R36.

## The source

- API root: `https://api.viewsforecasting.org/`. No key. Returns
  `{"runs": [...]}`, 91 names on 2026-09-30, most of them old or
  experimental. Production runs are `fatalities<model>_<year>_<month>_t<n>`;
  newest then was `fatalities003_2026_08_t01`.
- Data: `<run>/<loa>/<violence>`. The adapter reads `cm/sb` (country-month,
  state-based). `pgm` is PRIO-GRID cells (about 470k rows a run, not
  loaded); the 2026-08 run's model tree lists nothing under `ns`/`os`/`px`.
- Paging: `pagesize` up to at least 10000. A full cm run is 6,876 rows
  (191 countries x 36 months, September 2026 to August 2029), one 1 MB page,
  about 8 s. `next_page` is an empty string on the last page.
- Each row: `isoab` (ISO3), `gwcode`, `name`, `year`, `month`,
  `month_id`, `main_mean` (predicted deaths), `main_mean_ln` (its log),
  `main_dich` (a probability, 0 to 1; VIEWS describes it as the chance of
  25+ deaths in the month, not verified here).
- Licence: no data licence stated; model code is CC BY-NC 4.0. Credit
  VIEWS and keep it non-commercial.

## Traps

- **The root lists every run ever, including `escwa_*`, `d_*`, `f_*`.**
  Only `fatalities*` names are production; pick by (year, month), then
  model number. `newest_run()` does this.
- **`main_mean` is expected deaths, heavily skewed.** Ukraine forecast
  4,062 for September 2026, the next country 267. Tint on a log scale.
- **A run's month is its data cutoff, not its publish date**: the
  `2026_08` run forecasts from September 2026.

## The stored copy

`conflict_forecast(run, country, month, fatalities, p_any)`, country as
FIPS via `country_code.iso3`. Every run is kept, so forecasts can be
compared across runs. All 191 countries mapped on the first load.

```sql
-- Next month's forecast from the newest loaded run, top 10.
WITH latest AS (
  SELECT file FROM fetch_log f JOIN source s ON s.id = f.source_id AND s.name = 'views-forecast'
  WHERE file LIKE 'fatalities%' ORDER BY fetched_at DESC LIMIT 1)
SELECT country, round(fatalities) AS deaths, round(p_any::numeric, 2) AS p
FROM conflict_forecast
WHERE run = (SELECT file FROM latest) AND month = date_trunc('month', now())::date
ORDER BY fatalities DESC LIMIT 10;

-- How one country's forecast for a fixed month moved across runs.
SELECT run, round(fatalities) FROM conflict_forecast
WHERE country = 'SU' AND month = '2026-12-01' ORDER BY run;
```

Served at `/api/forecast?months=1`. Polled daily by `ingest.py slow`; a run
already loaded logs a `poll:` row.
