# Ingestion expansion

Status: approved 2026-09-24. Extends `specs/news-radar.md`;
requirement and test numbers continue from it.

## Why

The map felt stale and noisy, and the diagnosis was not volume. GDELT Events
is ~150k events/day, but each row is a snapshot of its first 15 minutes, it
codes only CAMEO political interactions at one location, and its
`NumSources` ranks wire syndication (21 Newsquest papers reprinting one PA
story) above everything else. Articles also trail real events by 15 to 60
minutes, so news coverage is the slow channel for anything acute.

## Goal amendment

The product is two things over one store:

1. **Geo trends at a scale the user picks.** Time window from hours to
   months, level country or ADM1, measured as share of attention and its
   change. Needs a growth signal (Mentions) and a subject dimension (GKG
   themes).
2. **Acute events.** Primary sources lead, because they are ground truth
   with minutes of latency. Coverage and chatter confirm that the world is
   noticing. Detection stays content-blind (R5, R11).

The UI for both (window and level controls, layers, drill-down) is the next
spec, not this one.

## Three kinds of input

| Kind | What it is | Examples | Enters attention series |
|---|---|---|---|
| Coverage | News articles about places | GDELT Events, Mentions, GKG | yes |
| Ground truth | An authority saying something happened | USGS, GDACS, NWS, tsunami, HANS, FIRMS, IODA, Radar | no, own layers |
| Chatter | Counted, never read, mentions of places in a public stream | Wikipedia, Bluesky | no, own rate series |

## Requirements

### Coverage

- **R18 GDELT Mentions every 15 min.** Same `lastupdate.txt` cycle as R2,
  https only. Measured 2026-09-24 at a quiet slot: 2,409 rows, 0.5 MB per
  file, so roughly 250k to 400k rows/day. Raw rows (event, time, outlet)
  are kept only as a 72 h buffer, because distinct outlets per hour cannot
  be summed across 15-minute files (changed at build, 2026-09-24). The
  buffer rolls into `mention_hourly(event_id, hour, mentions, sources)`,
  where sources counts distinct `MentionSourceName`. Mentions of events not in
  `event` (older than 90 days, or no geo) are counted in `fetch_log` and
  dropped.
- **R19 Attention counts coverage over time, not first sight.**
  `attention_hourly.mentions` and `.sources` are recomputed from
  `mention_hourly` joined to the event's region, so a story that keeps
  running keeps its place lit. `events` stays first-sight. Anomaly (R5) and
  the share z-score read the new column unchanged. Backfill: 30 days of
  Mentions files, the same way R3 backfilled Events.
- **R20 Growth per event.** `event_growth` view: mentions and distinct
  sources in the last 1 h and 6 h against the first window. This is the
  "still building" signal the detector and the story list read. It replaces
  `NumSources` at first sight as the story ranking.
- **R21 Syndication collapse.** A story is the set of event URLs sharing a
  normalized headline (lowercased, punctuation and site suffix stripped)
  within 48 h. Story-level counts use distinct sites, so 21 reprints of one
  PA story count as one story with 21 sites, not 21 stories. Computed at
  read time from `story` by `top_stories(hours, limit)`. No
  publisher-network table in this spec, so a syndicated story still counts
  each network paper as its own outlet; collapse only stops it appearing as
  many stories. R16's headline fetch also picks URLs whose event reached 2+
  distinct outlets in the buffer, so a story that grows late gets a title.
- **R22 GKG, slim.** GKG 2.1 every 15 min. Measured: 875 articles, 11.5 MB
  raw per quiet file, about 1 GB/day raw, so raw is never stored. Kept per
  article: `gkg_article(id, added_at, url, site, tone, themes text[])` and
  `gkg_location(article_id, geom, country, adm1, loc_type)` from
  `V2Locations` (every place in the article, not one action geo). Raw
  retention 30 days. Rolled into `theme_daily(region_kind, region, theme,
  day, articles)`, kept forever, and `theme_hourly` at country level, kept
  90 days. Location type 1 (country-only centroids) counts toward country
  but never ADM1.
- **R23 Retention is measured before it is final.** Phase 1 ends by
  reporting the real per-day size of `gkg_article`, `gkg_location`,
  `theme_daily` and `mention_hourly` after 7 days. The 30 d and 90 d numbers
  above are defaults until then.

### Ground truth

All are feed adapters like R17: fetched whole, upserted, `attention =
false`, reverse geocoded by `country_shape` when they carry a point, and
drawn as their own layer. Revisions update `props` in place unless noted.

- **R24 NWS alerts.** `https://api.weather.gov/alerts/active`, User-Agent
  required (empty UA returns 403), public domain. Measured: 475 active, 93%
  with null geometry, only UGC and SAME zone codes. So the adapter joins a
  local `nws_zone(url, code, kind, name, geom)` cache: a zone's polygon is
  fetched from `api.weather.gov/zones/...` the first time an alert names
  it and refetched after 90 days (changed at build, 2026-09-24: the
  shapefile route needs GIS tooling on every self-hosted install). An
  Update is a new id with `references[]`; the store keeps the chain and
  marks superseded rows, it does not overwrite. US only, which is fine:
  this is ground truth where it exists.
- **R25 Tsunami.** NTWC `PAAQAtom.xml` (covers the US West Coast and
  Alaska) and PTWC `PHEBAtom.xml`. Each holds only the latest bulletin, so
  polling every 5 min and keeping every new `urn:uuid` is the history.
  Point from `geo:lat`/`geo:long`.
- **R26 Volcanoes.** USGS HANS `getElevatedVolcanoes` and
  `notice/getNewestOrRecent` (about 5 notices/day, keyed on
  `noticeIdentifier`), points from `vsc/api/volcanoApi/elevated`. A colour
  code change is an event; a notice at the same level is `props` history.
- **R27 FIRMS fires, clustered.** The three VIIRS 24 h global CSVs (SNPP,
  NOAA-20, NOAA-21), no key needed. Measured about 180k detections/day,
  4.5 to 4.8 MB per file. No id: raw rows go to `firms_detection` keyed
  `(satellite, lat, lon, acq_date, acq_time)`, kept 30 days. Detections
  cluster with `ST_ClusterDBSCAN` (eps 1 km, minpoints 1) over a rolling
  48 h into `fire` events. A cluster that touches an existing fire extends
  it; one that does not is a new fire. `props` carry detection count and
  summed FRP (fire radiative power, the satellite's measure of how
  intensely it burns). MODIS is skipped as redundant with VIIRS.
- **R28 IODA outages.** `api.ioda.inetintel.cc.gatech.edu/v2/outages/events`,
  no auth. Only `country` and `region` entities are stored; ASN-level rows
  are most of the volume and dropped. No ids: key
  `(datasource, entity fqid, start)`, revised in place as duration grows.
  No points: regions join Natural Earth admin-1 by `ne_region_id`, and the
  layer draws the polygon rather than a dot. The data licence is
  unpublished, so the README says so and nothing republishes it.
- **R29 Cloudflare Radar outages.** `radar/annotations/outages`, needs a
  free account token with Radar Read, set in `.env`. Without the token the
  adapter is disabled and logs once. Data is CC BY-NC 4.0; the README says
  so. Overlaps IODA and is kept because it names cause (power, cable cut,
  government shutdown) where IODA does not.

### Chatter

Both are long-running stream consumers, a new adapter shape beside file
and feed: a `systemd --user` service with `Restart=always`, resuming from a
stored cursor. Neither stores content, only counts per place. That keeps
detection content-blind and keeps third-party text out of the store.

- **R30 Wikipedia.** EventStreams `recentchange` and `page-create`, all
  Wikipedia language editions, namespace 0 only; Wikidata, Commons and
  other projects are discarded client-side (there is no server filter, so
  the whole ~5 GB/day stream is read). Resume by `Last-Event-ID`. Per page
  per hour: `wiki_activity(wiki, title, hour, edits, editors, created)`.
  Coordinates come from the GeoData API (`prop=coordinates`, batched 50
  titles per call per wiki) and are cached in `wiki_page(wiki, title,
  geom, country, adm1, checked_at)`; a page with no coordinates is cached
  as such and rechecked after 7 days. Measured: 2 of 18 sampled edited
  pages had coordinates, one of them a school shooting article created
  minutes after the event. Only geotagged pages enter the rate series.
- **R31 Bluesky.** Jetstream `app.bsky.feed.post`, no auth, zstd
  compression on. Measured off-peak: 26 posts/s, about 2.2M posts/day and
  1.9 GB/day uncompressed. Posts carry no geo, so place is found by
  gazetteer match, not a model: GeoNames `cities15000` plus country and
  ADM1 names, including alternate names in the post's declared `langs`.
  Stored: `chatter_5min(source, place_id, bucket, posts)` and up to 20 post
  `at://` URIs per place per bucket for drill-down, fetched live on
  display. Text is never stored. A delete event removes its URI.
  Ambiguous names ("Georgia", "Jordan", "Paris") are kept, because the
  detector scores each place against its own baseline, so a name that is
  always noisy only fires when it is unusually noisy. The gazetteer reloads monthly from GeoNames' daily-updated dump. The
  GeoNames licence is CC BY 4.0, credited in the README. No model call, local or remote.

### Operations

- **R32 Every source is visible when it stops.** Every adapter writes
  `fetch_log` (stream consumers once per hour of consumption), and
  `source_health` view gives last success and rows per source. A crashed
  unit is already covered by failed-systemd-unit alerting on the incident
  path; a source that runs but returns nothing shows in `source_health` and
  on the map's source list. Neither path is a new notifier.
- **R34 One skill per source.** Each source has a skill at
  `.claude/skills/<source>/SKILL.md`, committed with its adapter: how to
  reach the source directly (endpoint, auth, format, ids, revisions,
  licence, measured volume, traps) and how to read our stored copy (tables,
  a working SQL query, freshness). The adapter code stays the authority; the
  skill cites its constants rather than restating logic. GDELT, USGS and
  GDACS get theirs now, since they are already live.
- **R33 Bandwidth is stated.** Steady state is about 7 GB/day down
  (Wikipedia ~5, Bluesky ~1 to 2 compressed, GKG ~0.35, the rest small),
  about 210 GB/month.

## Not in this spec

The explorer UI (window, level, layer toggles, drill-down), detector
thresholds (phase 4, now reading R20, R24 to R31), curated RSS, ReliefWeb
(API needs an approved appname; RSS is context only), WHO outbreak news
(days of latency), EONET (re-aggregates FIRMS, IRWIN and NHC), Smithsonian
GVP (weekly), MODIS.

## Schema additions

```
mention_hourly    event_id, hour, mentions, sources   PK (event_id, hour)
event_growth      view: 1 h and 6 h mentions/sources vs first window
gkg_article       id, added_at, url, site, tone, themes text[]     30 d
gkg_location      article_id, geom, country, adm1, loc_type         30 d
theme_daily       region_kind, region, theme, day, articles         forever
theme_hourly      region, theme, hour, articles (country)           90 d
nws_zone          code, kind, geom
firms_detection   satellite, lat, lon, acq_date, acq_time, frp, confidence  30 d
wiki_page         wiki, title, geom, country, adm1, checked_at
wiki_activity     wiki, title, hour, edits, editors, created        90 d
gazetteer_place   id, name, alt_names, kind, geom, country, adm1
chatter_5min      source, place_id, bucket, posts, uris text[]      90 d
source_health     view
event             + superseded_by (NWS chains)
```

## Tests

| ID | Traces | Asserts |
|---|---|---|
| T17 | R18 | Fixture Mentions file rolls to hand-computed `mention_hourly`; mentions of unknown events are counted and dropped; re-ingest adds nothing |
| T18 | R19 | A fixture event mentioned over 5 hours lights its region in all 5 hours of `attention_hourly`, not only the first |
| T19 | R20 | `event_growth` on synthetic mentions: a rising event and a flat one, hand-checked |
| T20 | R21 | Four fixture headlines from three sites, two of them identical after normalization, collapse to three stories with correct site counts |
| T21 | R22 | Fixture GKG row yields one article, the listed themes, and one `gkg_location` per `V2Locations` entry; type 1 never lands in ADM1 |
| T22 | R24 | Null-geometry fixture alert geolocates through `nws_zone`; an Update marks its reference superseded |
| T23 | R25 | Atom fixture parses to a point event; the same uuid twice inserts once |
| T24 | R26 | HANS fixtures: a colour change is an event, a same-level notice updates `props` |
| T25 | R27 | Synthetic detections 500 m apart cluster to one fire, 5 km apart to two; a next-day adjacent detection extends the first |
| T26 | R28 | IODA fixture keeps country and region rows, drops ASN rows, and a longer revision updates in place |
| T27 | R29 | With no token the adapter is disabled and inserts nothing; with a fixture response it parses cause and country |
| T28 | R30 | Stream fixture: non-Wikipedia and non-ns0 events dropped; hourly counts and distinct editors correct; resume id stored |
| T29 | R31 | Gazetteer match on fixture posts in two languages; a delete removes the URI; no post text reaches the database |
| T31 | R34 | Every adapter module has `.claude/skills/<KIND>/SKILL.md` named for its `KIND`, and the adapter's endpoint constant appears verbatim in it |
| T30 | R32 | A source with no rows for 3 of its intervals shows stale in `source_health` |

## Phases and stop points

1. Skills for the three live sources (R34), `source_health` (R32) so the
   new sources arrive with a health view, then Mentions (R18 to R20),
   syndication collapse (R21), GKG (R22), 30-day Mentions backfill. T17 to
   T21, T30, T31. **Stop: 7-day measured sizes, confirm
   retention (R23).**
2. Ground truth R24 to R29, one adapter per commit. T22 to T27. Radar lands
   disabled until a token is in `.env`.
3. Chatter R30 and R31. T28, T29.

Every adapter commit from phase 1 on carries its skill (R34).

## Assumptions stated

- The store keeps no post text from any source; Bluesky and Wikipedia are
  counts plus pointers.
- ~7 GB/day of download is acceptable on this connection.
- IODA is ingested for personal use with its licence unverified.
- Cloudflare Radar needs you to create a free account token; until then
  its adapter is off.
