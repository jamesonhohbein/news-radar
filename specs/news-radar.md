# news-radar

Two-tier news intake replacing Reddit and Perplexity's news tab. Tier 1
(**acute**) pushes to the phone when many independent outlets report the same
thing at once. Tier 2 (**trends**) is an always-current Grafana surface showing
what the world is talking about and what is new this week. There is no digest,
no ranked reading list, and nothing to scroll.

Sibling of `~/dev/homelab/ai-radar` and follows its design rule: the LLM
extracts, it never judges. Nothing is dropped for being unimportant and there
is no interest profile. The acute trigger is content-blind by construction.

## Requirements

- **R1 Sources are fixed and curated.** `feeds.txt` (ai-radar format) is the
  entire source set. No social feeds, no aggregators that rank by engagement.
  Starter set of ~20 (wire, national, regional, primary) validated reachable at
  execute time with `load_feeds.py --check-only`; dead URLs are dropped, not
  guessed around.
- **R2 Polling reuses ai-radar's Miniflux** under a second Miniflux user
  `news`, created once via `POST /v1/users` with the admin credentials. Its
  feeds are invisible to ai-radar's `ingest.py`, which lists the admin user's
  feeds only, so ai-radar's code and Opus spend are untouched. Nobody opens the
  UI.
- **R3 Storage is its own Postgres** (`news-radar-db`, postgres:17-alpine,
  `127.0.0.1:5439`, joined to the claude-telemetry compose network so Grafana
  reaches it by alias). One datasource, one database, uid `news`, mounted as a
  single file the way `radar-datasource.yaml` is. `news.item` is the record;
  Miniflux retention is not relied on. History only grows forward.
- **R4 Ingest is deterministic and cheap.** `ingest.py` every 15 min: Miniflux
  `/v1` → upsert on `miniflux_id` → embed `title + first 300 chars` with
  `nomic-embed-text` on the system ollama (`:11435`) → cluster. No LLM call in
  this path, so a model outage cannot delay an acute alert.
- **R5 Clustering is the corroboration primitive.** Union-find over pairs with
  cosine ≥ 0.80 whose `published_at` are within 48 h. A cluster's identity is
  its lowest item id, so it is stable across runs and re-embedding. A cluster
  row carries `first_seen`, `last_seen`, `source_count` (distinct feeds),
  `item_count`, and `headline` (title of the earliest item). Threshold 0.80 is
  the starting value and T2 fixes it against real fixtures.
- **R6 The acute rule is a Grafana rule in a `News` folder** in
  `~/Software/claude-telemetry/alerting/95-news.yaml`, per the machine-wide
  one-incident-path rule. Multi-dimensional: one instance per cluster with
  `source_count ≥ N` reached within 3 h of `first_seen` and `first_seen` in the
  last 6 h. Labels `severity=warning`, `cluster_id`; annotation
  `summary={{ $labels.headline }} ({{ $labels.source_count }} outlets)`.
  `for: 0s`; the corroboration window is the debounce. One push per cluster,
  resolves when the cluster ages out of the 6 h window.
- **R7 The rule ships paused.** N is not guessed: `scripts/replay.py --days 7`
  prints every cluster that would have fired for N in 3..8 after a week of
  real polling, and N is chosen from that output at a stop point. Until then
  `isPaused: true`.
- **R8 Trend extraction mirrors ai-radar.** `extract.py` daily: entities of
  kind `place | org | person | event` per item, joined through `item_entity`.
  Model chosen by `--estimate` at execute time (Haiku 4.5 default; the inputs
  are headlines and ledes, not abstracts). Idempotent on `extracted_at`.
- **R9 Surface is a Grafana dashboard `news-trends`** in the `News` folder:
  (a) live clusters, last 48 h, ranked by `source_count`; (b) volume per
  category per day; (c) entities rising week-over-week; (d) entities first
  seen this week against the full archive; (e) per-feed volume and
  `parsing_error_count` so a silently dead feed is visible.
- **R10 Watch layer is designed, not built.** A `watch` table
  (`pattern`, `kind`, `min_sources`) and a second rule that fires on any
  cluster matching a watched entity at a lower N. Schema reserves the table;
  no rows, no rule, until the region/topic use case arrives.
- **R11 Syndication counts as corroboration.** Five outlets running one AP
  story are five editors deciding it leads; that is the magnitude signal
  wanted, not a bug to dedupe away. Documented, not worked around.
- **R12 Both timers are `systemd --user`** with `Persistent=true`, unit files
  in `systemd/` symlinked into `~/.config/systemd/user/`, same as ai-radar.
  `ANTHROPIC_API_KEY` from the user manager environment.

## Non-goals

Summaries of individual articles. A reading queue. Any score, interest
profile or thumbs. A second notifier. Migrating ai-radar into this repo.

## Schema (news database)

```
item          id, miniflux_id UNIQUE, title, url, feed, category,
              published_at, content, embedding vector(768),
              cluster_id BIGINT NULL, extracted_at, inserted_at
cluster       id (= min item id), first_seen, last_seen, source_count,
              item_count, headline, updated_at
entity        id, key UNIQUE, display, kind
item_entity   (item_id, entity_id) PK
watch         id, pattern, kind, min_sources, created_at   -- R10, empty
feed_health   view over item: per feed, items/day, last item
live_cluster  view: clusters with first_seen > now() - 48h
```

`pgvector` for `embedding`; clustering compares only against items in the
48 h window, so the index is a nicety, not a requirement.

## Starter feeds (validated at execute; dead ones dropped)

Wire/global: BBC World, Guardian World, NPR News, Al Jazeera, DW, France 24,
NYT World, WaPo World, CBC World, ABC (AU) Just In, UN News.
Primary: USGS significant earthquakes, GDACS, Federal Reserve press releases,
WA Emergency Management. Regional: Seattle Times, Cascadia Daily News,
Bellingham Herald, KUOW.

Primary feeds are single-source and will not corroborate on their own; they
exist for the trend tier and the future watch layer.

## Tests

| ID | Traces | Asserts |
|---|---|---|
| T1 | R4 | `ingest.py` re-run inserts zero new rows and changes no cluster |
| T2 | R5 | Fixture of 12 real headlines (3 events × 4 outlets) yields exactly 3 clusters; 12 unrelated headlines yield 12 |
| T3 | R5 | A cluster's id is unchanged after adding a later item to it |
| T4 | R6 | Rule `rawSql` loaded from the YAML, run on synthetic rows: 5 feeds in 2 h fires, 5 feeds over 5 h does not, 2 feeds in 1 h does not |
| T5 | R6 | Rule YAML carries `severity` label and `summary` annotation |
| T6 | R7 | `replay.py` output for a synthetic week matches T4's hand count |
| T7 | R8 | `extract.py` re-run inserts zero `item_entity` rows |
| T8 | R1 | `load_feeds.py --check-only` exits non-zero on an unreachable URL |

T4 uses the pse test pattern: the provisioned SQL is what runs, not a copy.

## Phases and stop points

1. Repo, compose, schema, Miniflux user, feeds, `ingest.py` with clustering,
   timers. Tests T1-T3, T8. **Ends with a week of polling.**
2. Replay over that week. **Stop: choose N from the replay table.** Then
   `95-news.yaml` unpaused, T4-T6, first live push.
3. `extract.py`, dashboard, T7. **Stop: review dashboard, prune feeds by
   entities contributed** (ai-radar's rule: judge a feed on entities, not
   entry count).

## Assumptions stated

- Cosine 0.80 on `nomic-embed-text`; T2 may move it.
- 15-min ingest against Miniflux's 30-min poll, so worst-case lag to a push is
  ~45 min plus Grafana's evaluation interval (1 m).
- Regional feeds count toward N. If they inflate local-only stories past N,
  the fix is a per-category weight, decided at the phase 2 stop.
