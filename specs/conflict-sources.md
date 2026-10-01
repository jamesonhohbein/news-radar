# Conflict sources

Status: approved and built 2026-09-30. Extends `specs/ingestion-expansion.md`;
requirement and test numbers continue from it.

## Why

GDELT codes conflict (CAMEO roots 18 to 20) but counts coverage, not
violence: one shelling reported by 200 outlets is 200 mentions and no
fatality figure. FIRMS (R27) shows heat, not cause. Neither says who fought
whom or how many died, and nothing in the store says where violence is
expected next. UCDP answers the first, VIEWS the second. Both are open and
keyless.

## Requirements

- **R35 UCDP candidate events.** CSVs from
  `https://ucdp.uu.se/downloads/candidateged/`, no key. The UCDP API
  (`ucdpapi.pcr.uu.se`) answers 401 without an access token (measured
  2026-09-30), so the adapter uses the downloads. Discovery: fetch
  `https://ucdp.uu.se/downloads/`, take every `candidateged/GEDEvent_*.csv`
  link, skip names already in `fetch_log`. Two release shapes, measured:
  a monthly file (`v26_0_8`, August 2026: 1,806 events, 1.4 MB,
  `date_end` to 2026-09-10) and a consolidated file (`v26_01_26_06`, January to
  June: 10,051 events, 7.8 MB).
  - One `event` per UCDP `id`, kind `ucdp`, `attention = false`, drawn as
    its own layer. Ids are stable across releases (14 events appear in both
    the July and August files, revised), so a later file upserts and
    updates `props` in place.
  - `occurred_on` is `date_start`. `added_at` is the fetch time of the
    first file carrying the event, since UCDP publishes no per-event
    timestamp. Point from `latitude`/`longitude`, reverse geocoded by
    `country_shape` like the other ground truth.
  - `props`: `type_of_violence` (1 state-based, 2 non-state, 3 one-sided
    against civilians), `conflict_name`, `dyad_name`, `side_a`, `side_b`,
    `best`/`low`/`high` deaths, `deaths_civilians`, `where_prec` (1 exact
    to 7 country-level), `date_prec`, `date_end`, `code_status`,
    `source_office`, `source_headline`.
  - Rows whose `code_status` is not `Clear` (43% of August) are kept and
    carry the status: the store is judgment-free. The layer styles them,
    it does not hide them.
  - Backfill is every 2026 candidate file on the page. Measured on first
    load: 13,685 rows upserted into 13,659 events (26 ids recur across
    releases), dated 2026-01-01 to 2026-08-31, 4 left without a country.
  - Changed at build: `primary_live` shows UCDP events from the last 60
    days by event date (1,632 on first load), drawn sized by `best`, and
    `/api/events/primary` returns up to 5,000 rows instead of 1,000 so they
    do not crowd out the other layers.
- **R36 VIEWS forecasts.** `https://api.viewsforecasting.org/`, no key. The
  root lists runs; the adapter takes the newest `fatalities*` run
  (measured: `fatalities003_2026_08_t01`) and skips runs in `fetch_log`.
  Country-month, state-based only (`/cm/sb`), 36 months ahead: 6,876 rows
  per run (191 countries x 36), paged. Stored in
  `conflict_forecast(run, country, month, fatalities, p_any)`, PK
  `(run, country, month)`, kept forever (about 7k rows a month).
  `fatalities` is `main_mean`. `p_any` is `main_dich`, a probability.
  Verified at build: 1,253 of 6,876 rows are above zero, so it is kept.
  `country` is FIPS, mapped from `isoab` through the GeoNames country codes
  table; unmapped rows are counted in `fetch_log` and dropped. Not an
  event and not attention. `/api/forecast?months=1` serves next month's
  figure per country, drawn as a country tint layer. Changed at build: the
  map has a two-way tint toggle (attention, conflict forecast), since both
  are country fills; forecast deaths are log-scaled onto the same ramp in
  purple.
- **R37 Daily slow poll.** `ingest.py slow` runs both adapters from a new
  `news-radar-slow.timer`, daily. Both publish on no fixed day; a run with
  nothing new is one page fetch per source. `source_health` marks each
  stale after 36 days without a new release (changed at build:
  `expect_every` is 12 days, because stale is three intervals).
- **R38 Skills.** `.claude/skills/ucdp/SKILL.md` and
  `.claude/skills/views/SKILL.md` per R34. The README credits both:
  UCDP is CC BY 4.0. VIEWS states no data licence (checked at build);
  its model code is CC BY-NC 4.0, so the README says to treat the
  forecasts as non-commercial.

## Schema additions

```
conflict_forecast  run, country, month, fatalities, p_any   PK (run, country, month)
source             + ('ucdp', 'ucdp-candidate', false), ('views', 'views-forecast', false)
```

## Tests

| ID | Traces | Asserts |
|---|---|---|
| T32 | R35 | Fixture downloads page yields only the `candidateged` CSV links, not `ged261` or `vpp` |
| T33 | R35 | Fixture CSV rows parse to point events with the listed `props`; a second file with the same `id` and changed `best` updates in place; a non-`Clear` row is stored |
| T34 | R36 | Newest `fatalities*` run is picked from a fixture run list; a two-page fixture stores 36 months per country with FIPS codes; an unmapped ISO code is counted and dropped; the same run twice inserts nothing |
| T35 | R37 | Both sources appear in `source_health` and go stale after 36 days |
| T10 | R36 | `/api/forecast` serves the newest run, sorted, months clamped to 36 (Playwright, `web/tests/globe.spec.ts`) |

T31 already covers R38.

## Not in this spec

ACLED (the public tier has no event-level data or API), POLECAT (machine
coded like GDELT; revisit if GDELT's conflict slice proves too noisy),
final GED history 1989 to 2025 (`ged261`, about 350k events, needed only
for a deaths-versus-norm view), VIEWS PRIO-GRID forecasts (about 470k rows
a run), and any detector reading these layers.

## Assumptions stated

- `added_at` is our fetch time for UCDP, so the January to June backfill
  lands at one timestamp.
- Non-`Clear` UCDP rows are kept.
- VIEWS at country level only.
