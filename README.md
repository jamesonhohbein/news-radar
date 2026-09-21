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

Then on a schedule: `ingest.py catchup` every 15 minutes and `rollup.py`
hourly. `systemd/` has user units; symlink them into
`~/.config/systemd/user/` and `systemctl --user enable --now` both timers.

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
| `event` | One row per geolocated event. GDELT's vocabulary: CAMEO code, QuadClass, Goldstein, tone, actors, action geo, and the mention/source/article counts from the 15-minute window it first appeared in |
| `attention_hourly`, `attention_daily` | Events, mentions and sources per country and per ADM1. Hourly kept 90 days, daily forever |
| `attention_anomaly` | Each of the last 48 hours against the same hour of day over the trailing 30 days: mean, sd, z. Materialized hourly by `rollup.py` |
| `fetch_log` | Every file loaded, with rows seen and kept. A gap here is a gap in the series |
| `source`, `alert`, `watch` | Adapter registry; what the detector has sent (later phase); reserved |

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

An adapter is a module in `newsradar/adapters/` exposing `latest()`,
`list_files(since)`, `fetch(file)` and `parse(file, blob)` yielding
`Event`s, plus a row in `source`. It never touches the database. `gdelt.py`
is the worked example; an `rss` adapter is the planned second one.

## License

Apache-2.0.
