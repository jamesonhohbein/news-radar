# news-radar

A world map, a chat box, and an agent. Sources feed a judgment-free store of
geolocated events; the map shows where the world's attention is and where it
has just jumped; the chat answers questions by choosing how to render the
answer. Acute events reach your phone; long-term trends are series over the
same store. Nothing is scored, ranked or filtered for you.

Status: **phase 2, the map.** GDELT 2.0 Events ingest every 15 minutes,
attention rollups per country and ADM1, and a Next.js world map showing each
country's share of the world's attention over the last 24 h against its
30-day norm, with the top stories as points. No chat, no pushes yet.
`specs/news-radar.md` is the design and the phase plan.

## Run it

```bash
cp .env.example .env            # set POSTGRES_PASSWORD
docker compose up -d --build    # Postgres 17 + PostGIS + pgvector on 127.0.0.1:5443
docker exec -i news-radar-db psql -U newsradar -d newsradar -v ON_ERROR_STOP=1 < schema.sql
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python ingest.py backfill --days 30   # ~180 MB, a few minutes
.venv/bin/python rollup.py --full
```

Then `scripts/load_countries.py` and `scripts/load_country_codes.py` once, and on a schedule: `ingest.py
catchup`, `headlines.py` and `ingest.py primary` every 15 minutes, and
`rollup.py` hourly. `systemd/` has user units; symlink them into
`~/.config/systemd/user/` and `systemctl --user enable --now` the three
timers (`ingest`, `rollup`, and `fast`, which polls tsunami bulletins every
5 minutes). `news-radar-wiki.service` and
`news-radar-bsky.service` are long-running consumers of Wikipedia's edit
stream and Bluesky's Jetstream, and `news-radar-monthly.timer` reloads the
GeoNames country codes and gazetteer; enable them the same way. Before the
first Bluesky run: `scripts/load_gazetteer.py`, then
`bsky_stream.py --calibrate 900`.

The map:

```bash
scripts/create-reader.sh                 # read-only role; READER_PASSWORD in .env
cd web && npm install && npm run build
PORT=3087 HOSTNAME=127.0.0.1 npm run start   # or systemd/news-radar-web.service
```

`NEXT_PUBLIC_MAP_STYLE` overrides the basemap (default: OpenFreeMap
positron, no key). `/api/anomaly`, `/api/events/top` and
`/api/attention/daily` are the page's only data paths and are plain JSON.

## What is in the database

| Table | Holds |
|---|---|
| `event` | One row per geolocated event. GDELT's vocabulary: CAMEO code, QuadClass, Goldstein, tone, actors, action geo, and the mention/source/article counts from the 15-minute window it first appeared in. Primary-feed events use the same row with `props` for what GDELT has no column for (magnitude, alert level) |
| `country_shape` | Natural Earth 110m polygons by FIPS code, for reverse geocoding sources that give a point but no country. Loaded by `scripts/load_countries.py` |
| `attention_hourly`, `attention_daily` | Events, mentions and sources per country and per ADM1. Hourly kept 90 days, daily forever |
| `attention_anomaly` | Each of the last 48 hours against the same hour of day over the trailing 30 days: mean, sd, z. Materialized hourly by `rollup.py` |
| `fetch_log` | Every file loaded, with rows seen and kept. A gap here is a gap in the series |
| `story` | Headline and site per story URL, fetched from the page after each ingest for URLs at 2+ first-window sources; failures retry 3 times, 6 h apart |
| `source`, `alert`, `watch` | Adapter registry (`attention` says whether a source's events count as coverage); what the detector has sent (later phase); reserved |

Two facts about GDELT that shape everything above:

- `NumMentions`, `NumSources`, `NumArticles` are counted only within the
  15-minute window an event first appeared in. They measure the initial
  burst, not total coverage. That is what an acute signal wants.
- One real-world story is many GDELT events (each actor pair, action and
  location is its own row). The attention series aggregate by place for that
  reason; do not count event rows and call it "stories".

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

They create and drop a scratch database on the running server and apply
`schema.sql` to it, so what is tested is the production schema and the
production SQL. `cd web && npx playwright test` runs T10 against the running
service and the live database. Test IDs trace to requirements in the spec.

## Adding a source

An adapter is a module in `newsradar/adapters/` plus a row in `source`. It
never touches the database. Two shapes, documented in
`newsradar/adapters/__init__.py`: file adapters (`gdelt.py`) publish a
series of files and are loaded once each; feed adapters (`usgs.py`,
`gdacs.py`) publish one endpoint holding current state and are fetched
whole and upserted every run. Set `source.attention = false` for anything
that is ground truth rather than coverage, and leave `country` empty to
have it reverse geocoded.

## License

Apache-2.0.

## Data licences

The code is Apache-2.0; the data each adapter pulls keeps its source's
terms. GDELT, USGS, NWS, NOAA tsunami centres and NASA FIRMS are open or
public domain (FIRMS asks for citation). GeoNames (country codes) is
CC BY 4.0. Wikipedia content is CC BY-SA, but only edit counts are stored.
Bluesky posts belong to their authors; only per-place counts and, for 48 h,
a few post URIs are stored.
**IODA is "Copyright Georgia Tech Research Corporation. All
Rights Reserved"**: fine to read for your own instance, not to republish.
Cloudflare Radar (optional, needs a token) is CC BY-NC 4.0.
