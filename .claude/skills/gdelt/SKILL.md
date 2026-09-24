---
name: gdelt
description: Access GDELT 2.0 (Events, and later Mentions and GKG) directly or through news-radar's stored copy. Use for anything about what the world's news is covering, where, and how much; the attention series and anomaly scores; the story headlines; or debugging the GDELT ingest. Covers the file endpoints, the https trap, what NumSources really measures, the added_at versus occurred_on trap, and working SQL against the store.
---

# GDELT 2.0

Adapter: `newsradar/adapters/gdelt.py` (column order pinned there as
`COLUMNS`). Spec: `specs/news-radar.md` R2 to R5, R16;
`specs/ingestion-expansion.md` R18 to R22.

## The source

- Base: `https://data.gdeltproject.org/gdeltv2/`. No auth, free, no rate
  limit documented.
- `lastupdate.txt` lists the newest three files, one line each as
  `size md5 url`: `*.export.CSV.zip` (Events), `*.mentions.CSV.zip`,
  `*.gkg.csv.zip`. `masterfilelist.txt` lists every file since 2015 and
  drives backfill.
- **https only.** `lastupdate.txt` itself prints `http://` URLs. The http
  host answers 301 and a client that does not follow redirects saves an
  empty file, which looks exactly like GDELT being down. Rewrite the scheme.
- New files every 15 minutes, stamped `YYYYMMDDHHMMSS` in UTC.
- Files are tab-separated, no header. Events has 61 columns, Mentions 16,
  GKG 27.
- Measured 2026-09-24 at a quiet slot: Events 909 rows (60 KB zipped),
  Mentions 2,409 rows (77 KB), GKG 875 articles (3.7 MB zipped, 11.5 MB
  raw). Peak slots run about 1.7x that. About 92k to 150k geolocated
  events/day.

## What the numbers mean

- `NumMentions`, `NumSources`, `NumArticles` on an event row are counted
  only within the 15-minute window it first appeared in. They are a burst
  measure and never grow. Later coverage lives only in Mentions.
- `NumSources` counts outlets, and syndication networks inflate it: one PA
  wire story reprinted by 21 Newsquest papers scores 21. Do not rank "what
  matters" by it alone.
- `Day` (stored as `occurred_on`) is when the article says the event
  happened; anniversaries put it years back. `DATEADDED` (`added_at`) is
  when GDELT saw it. **Every time series uses `added_at`.**
- One article yields several events (actor pair x action x place). Group
  by `url` for stories.
- Country codes are FIPS 10-4 (`UK`, `RS` for Russia, `SF` for South
  Africa), not ISO.

## The stored copy

Postgres on `127.0.0.1:5443`, db `newsradar`. Ad hoc:
`docker compose exec -T db psql -U newsradar -d newsradar` from the repo.
The web app and agent use the read-only `reader` role (10 s timeout).

| Table | Holds | Kept |
|---|---|---|
| `event` (source `gdelt-events`) | one row per geolocated event | 90 d |
| `mention` (source `gdelt-mentions`) | raw (event, time, outlet), mentions of events we hold | 72 h |
| `mention_hourly` | mentions and distinct outlets per event per hour | 90 d |
| `event_growth` | view: mentions and outlets in the last 1 h and 6 h per event | |
| `gkg_article` (source `gdelt-gkg`) | per article: URL, site, tone, V1 themes | 30 d |
| `gkg_location` | every place an article names; type 1 is a country centroid and has no ADM1 | 30 d |
| `theme_daily` | articles per theme per country or ADM1 per day | forever |
| `theme_hourly` | articles per theme per country per hour | 90 d |
| `story` | headline and site per URL with 2+ first-window sources | |
| `attention_hourly` / `attention_daily` | events (first sightings), mentions and outlets (coverage that hour) per country or ADM1 | 90 d / forever |
| `attention_anomaly` | hourly z-score vs same hour over 30 d, last 48 h | |
| `fetch_log` | one row per loaded file | |

```sql
-- Is it fresh? Newest file and when it loaded (expect under 20 min old).
SELECT max(file), max(fetched_at) FROM fetch_log fl
JOIN source s ON s.id = fl.source_id AND s.name = 'gdelt-events';

-- Where is the world's attention over the last 24 h?
SELECT region, sum(mentions) AS m FROM attention_hourly
WHERE region_kind = 'country' AND hour > now() - interval '24 hours'
GROUP BY region ORDER BY m DESC LIMIT 10;

-- What is still building: most outlets in the last hour.
SELECT g.sources_1h, g.sources_6h, e.country, s.title
FROM event_growth g JOIN event e ON e.id = g.event_id
LEFT JOIN story s ON s.url = g.url AND s.status = 'ok'
ORDER BY g.sources_1h DESC LIMIT 10;

-- What a country's coverage is about today (GKG themes; TAX_ and WB_
-- families are taxonomies and dominate the long tail).
SELECT theme, articles FROM theme_daily
WHERE region_kind = 'country' AND region = 'UP' AND day = current_date
  AND theme NOT LIKE 'TAX\_%' ORDER BY articles DESC LIMIT 15;

-- Top stories with headlines, one per URL.
SELECT DISTINCT ON (e.url) e.num_sources, e.country, s.title, s.site
FROM event e JOIN story s ON s.url = e.url AND s.status = 'ok'
WHERE e.added_at > now() - interval '6 hours'
ORDER BY e.url, e.num_sources DESC LIMIT 20;
```

Timers: `news-radar-ingest.timer` (every 15 min: catchup, headlines,
primary feeds) and `news-radar-rollup.timer` (hourly). Logs:
`journalctl --user -u news-radar-ingest`.
