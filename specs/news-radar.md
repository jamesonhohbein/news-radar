# news-radar

A world map, a chat box, and an agent. Data sources feed a judgment-free store
of geolocated events; the map shows where the world's attention is and where it
has just jumped; the chat answers questions by choosing how to render the
answer (map layer, chart, or text). Acute events reach the phone as pushes;
long-term trends are series over the same store. Open source from day one,
self-hostable with `docker compose up`; the homelab integration is config, not
core.

Replaces Reddit and Perplexity's news tab as the news intake. Follows
ai-radar's rule: nothing in the store is scored, filtered or judged. The agent
is a lens on a complete store, applied on demand.

## Requirements

### Data

- **R1 Adapters, not feeds.** A source is an adapter that writes normalized
  `event` rows. Two shapes: file adapters (`gdelt`) publish a series of files;
  feed adapters (`usgs`, `gdacs`) publish one endpoint holding current
  state. `rss` and `gkg` come later behind the same module contract.
  Adapters are registered in `source`, enabled per instance.
- **R2 GDELT 2.0 Events every 15 min.** `lastupdate.txt` names the latest
  `export.CSV.zip`; the adapter stores every event with a resolvable
  `ActionGeo` point. Kept: GDELT id, occurred day, added timestamp, CAMEO code
  and root, QuadClass, Goldstein, tone, actor names and country codes, action
  geo (point, type, name, country, ADM1), `NumMentions`/`NumSources`/
  `NumArticles` at first sight, source URL. Idempotent on `(source, external_id)`.
  `masterfilelist.txt` drives backfill.
- **R3 Backfill 30 days on first run**, so attention baselines exist before
  the map is opened. Events only; the Mentions table is a later adapter.
- **R4 Attention is the derived series.** `attention_hourly(region, hour,
  events, mentions, sources)` and `attention_daily(...)` rolled up per country
  and per ADM1 from `event`. Daily is kept forever; hourly for 90 days; raw
  `event` for 90 days. History only grows forward.
- **R5 Anomaly is content-blind.** `attention_anomaly` view: for each region
  and hour, mentions z-score against the trailing 30 days of the same
  hour-of-day. This is the acute signal for *places*. The acute signal for
  *events* is `NumSources` at first sight. Neither reads content.
- **R6 Storage is Postgres 17 + PostGIS + pgvector** in the repo's compose,
  named volume (on the homelab host Docker's data root is the NVMe), `127.0.0.1:5443`, nothing joins it to the
  claude-telemetry network; Grafana is not in the loop. Read-only role
  `reader` with `statement_timeout=10s` for the agent.

- **R16 Stories carry headlines.** GDELT gives actor codes and a URL, not a
  title, so a dot reads "SAINT → None". `headlines.py` fetches the page
  `<title>` (falling back to `og:title`) for every event URL with
  `NumSources ≥ 2` seen in the last 24 h, into `story(url, title, site,
  status, attempts, fetched_at)`. Measured 2026-09-20: ~350 URLs/day at that
  threshold. One fetch per URL, 10 s timeout, 512 KB cap, a named
  User-Agent; failures retry up to 3 times, at least 6 h apart. Runs after
  every ingest. Adapters that carry titles natively (RSS) write `story`
  directly and skip the fetch.

- **R17 Primary feeds are ground truth, not coverage.** `usgs` (M4.5+
  quakes, GeoJSON) and `gdacs` (disaster alerts, RSS with georss points)
  are feed adapters: one endpoint holding current state, fetched whole and
  upserted every 15 min, because a quake's magnitude and an alert's level
  are revised in place. Neither gives a country, so both are reverse
  geocoded in PostGIS against the Natural Earth polygons (`country_shape`:
  containing polygon, else nearest within 100 km, else empty). Their events
  carry `props` (magnitude, depth, alert level, event type) and
  `source.attention = false`: they never enter the attention series, and
  the map draws them as their own layer (quakes sized by magnitude, GDACS
  alerts above Green). NHC was dropped: GDACS's tropical-cyclone class is
  global.

### Surface

- **R7 Next.js app** (`web/`): MapLibre GL 5, flat mercator by decision 2026-09-20 (the globe was built first and rejected on sight), native
  fill and circle layers (deck.gl only when a layer type needs it; its
  overlay on the globe projection is unproven), Vega-Lite charts from phase
  3, one page. Default view without any chat: last-24 h attention as a
  country choropleth by share-of-world z-score (built 2026-09-20; counts were
  tried first and read as "everything is red" on a peak-hour basis and
  "everything is below baseline" on a Sunday), top stories by `NumSources`
  as points (one per article URL), a 30-day daily sparkline and expected-vs-
  actual for the hovered region. Served on `127.0.0.1:3087` and `news.lan`.
- **R8 Chat drives rendering.** `/api/chat` runs Claude with two tools:
  `sql` (read-only role, LIMIT enforced, schema in the system prompt) and
  `render`, whose argument is typed JSON the client draws:
  `{kind:"map", layer:"points"|"heat"|"choropleth", features, legend}`,
  `{kind:"chart", spec: <Vega-Lite>}`, `{kind:"text", markdown}`. One turn
  may emit several renders; the map keeps the last map render as a layer
  until replaced or cleared.
- **R9 The agent sees the schema, not a curated tool list.** The system
  prompt carries the DDL and the CAMEO root-code and QuadClass legends so it
  can write its own queries. Typed convenience tools come only if `sql` is
  measured to fail on common asks.
- **R10 No auth in core.** The homelab deploy sits behind authentik forward
  auth like radar.jameson.casa, in the ingress repo, not here.

### Acute delivery

- **R11 Detection is app code.** `acute_firing` view applies `Z` and `N`
  from instance config and returns the rows that should be alerting now:
  (a) regions whose attention z-score over the last hour is ≥ Z, with the
  top event there; (b) events with `NumSources ≥ N` added in the last 3 h.
  `detector.py` polls it every 5 min, writes one `alert` row per new
  `(kind, key)`, and hands the row to the configured sinks. `alert.sent_at`
  is the dedup; a key that has fired is not re-sent for 24 h.
- **R12 Thresholds are unset until replayed.** `scripts/replay.py --days 30`
  tabulates what would have fired for Z in 3..6 and N in 20..100; Z and N
  are picked from that table at a stop point. With either unset the
  detector logs candidates and sends nothing.
- **R13 Sinks are pluggable.** Core ships `log` and `webhook` (generic JSON
  POST, payload documented in the README). The homelab instance's webhook
  target is a Home Assistant webhook automation that forwards to the iPhone
  companion app; that automation lives on the Pi, not in this repo.
  **This is a deliberate second notifier on hog, decided 2026-09-20**, an
  exception to the one-incident-path rule; the `alert` table is its record
  and nothing here writes to `alert_journal` or to Grafana.

### Project

- **R14 Public repo, Apache-2.0**, `github.com/jamesonhohbein/news-radar`.
  README is the product doc; `specs/` is the record. Nothing host-specific
  committed: `.env.example`, `docker-compose.override.yml` gitignored.
- **R15 Timers are `systemd --user`** with `Persistent=true` on the homelab
  host; the compose also ships a `scheduler` service running the same loop
  for self-hosters.

## Non-goals (v1)

Article summaries, a reading queue, any interest profile, GKG themes, the
Mentions table, RSS, user accounts, mobile layout, a Grafana rule.

## Schema

```
source            id, kind, config jsonb, enabled, created_at
event             id, source_id, external_id, occurred_on, added_at,
                  cameo_code, cameo_root, quad_class, goldstein, tone,
                  actor1_name, actor1_country, actor2_name, actor2_country,
                  geo_type, geo_name, country, adm1, geom geometry(Point,4326),
                  num_mentions, num_sources, num_articles, url
                  UNIQUE (source_id, external_id); GIST on geom; BRIN on added_at
attention_hourly  region_kind ('country'|'adm1'), region, hour, events,
                  mentions, sources          PK (region_kind, region, hour)
attention_daily   same shape, day
attention_anomaly view: hourly z-score vs trailing 30d same-hour baseline
acute_firing      view: rows the detector sends, so replay and detector share SQL
alert             id, kind, key, payload jsonb, created_at, sent_at, sink
watch             id, pattern, kind, min_sources    -- reserved, empty
```

## GDELT facts the code depends on

- Files are tab-separated, 61 columns, no header; column order is the v2
  codebook and is pinned in `adapters/gdelt.py` as a tuple.
- `NumMentions/NumSources/NumArticles` are counted within the 15-min window
  the event first appeared in; later coverage is only in Mentions. So
  `NumSources` is a *burst* measure, which is what the acute rule wants.
- One real-world event yields many GDELT events (actor pair × action ×
  location). The map aggregates by region; the event rule fires per GDELT id
  and the notification groups by `grafana_folder`, so one story is one push.
- Measured 2026-09-20: 30 days is 2,880 files, 179 MB zipped, 2.77M
  geolocated events (~92k/day), 1.1 GB in Postgres. Backfill ~45 min,
  bound by DNS and latency, not size; full rollup 6 s.

## Tests

| ID | Traces | Asserts |
|---|---|---|
| T1 | R2 | Parsing a fixture export file yields the pinned columns with correct types; events without geo are skipped and counted |
| T2 | R2 | Re-ingesting the same file inserts zero rows |
| T3 | R4 | Rollup over fixture events produces the hand-computed hourly and daily rows |
| T4 | R5 | Anomaly view: a region with flat baseline and a 5x hour yields z above 3; a flat region yields |z| below 1 |
| T5 | R11 | `acute_firing` on synthetic rows: region and event fire cases, and non-fire cases, each |
| T6 | R11 | `detector.py` run twice over the same rows sends once; the `alert` row carries the payload |
| T7 | R12 | `replay.py` on synthetic rows matches T5's hand count; with Z or N unset the detector sends nothing |
| T8 | R8 | `render` payloads validate against the JSON schema; an invalid payload is rejected before reaching the client |
| T9 | R8 | `sql` tool refuses non-SELECT and enforces LIMIT; a 20 s query is cut at 10 s |
| T11 | R16 | Title extraction on fixture HTML: `<title>`, `og:title` fallback, entity decoding, whitespace collapse, no title at all |
| T12 | R16 | Selection: URLs at `NumSources ≥ 2` in-window are picked, fetched rows are not re-picked, failed rows are re-picked only after 6 h and under 3 attempts |
| T13 | R17 | USGS fixture parses to typed events with magnitude, depth and UTC time, country empty |
| T14 | R17 | GDACS fixture parses with stable `{type}{eventid}` ids, alert level, and a modified timestamp |
| T15 | R17 | Reverse geocode: inland point gets its country, a point 40 km offshore gets the nearest, mid-ocean stays empty |
| T16 | R17 | A revised event updates `props` but keeps its geocoded country; primary events add nothing to `attention_hourly` |
| T10 | R7 | Playwright against the running service and live database: APIs return rows, params clamp, globe renders with both layers populated and no page errors |

## Phases and stop points

1. Repo, compose, schema, `gdelt` adapter, rollups, backfill, timers. T1-T4.
   **Ends when 30 days are in and the anomaly view returns rows.**
2. Web app with default layers, no chat. T10. **Stop: look at the map.**
   Done 2026-09-20; flat map by decision. Then the ingestion pass that was
   pulled ahead of chat and detection by decision the same day: headlines
   (R16, T11-T12) and primary feeds (R17, T13-T16), both done 2026-09-20.
   Still open in that pass: GDELT Mentions, GKG (needs a retention
   decision), curated RSS.
3. Chat with `sql` + `render`. T8-T9. **Stop: try ten real questions; decide
   whether typed tools are needed (R9).**
4. Detector with `log` sink, replay, **stop: pick Z and N**, HA webhook
   automation, `webhook` sink on, first push. T5-T7.
5. Publish: README, `.env.example`, license, `rss` adapter as the worked
   example of adding a source.

## Assumptions stated

- Regions are country and ADM1 as GDELT codes them; no own geocoding in v1.
  Display names come from GDELT's own country-level labels (`region_name`,
  refreshed by rollup) with Natural Earth 110m as the polygon source; a few
  microstates have a name but no polygon and appear only in the panel.
- The hourly detector signal still scores counts, not share (see CLAUDE.md);
  revisit before picking Z in phase 4.
- Z and N are unset until phase 4; nothing pushes before then.
- Repo name stays `news-radar` until publishing; renaming is one command.
- The only change outside this repo is one HA webhook automation on the Pi.
