---
name: bluesky
description: Access Bluesky public post activity (Jetstream firehose) directly or through news-radar's stored per-place counts. Use for anything about how much Bluesky is talking about a place right now, chatter bursts by city or country, finding a few recent posts about a place, the gazetteer and its stoplist, or debugging the news-radar-bsky consumer. Covers the Jetstream endpoint and cursor, why no post text is kept, how place names are matched and where that goes wrong, and working SQL.
---

# Bluesky chatter

Adapter and matcher: `newsradar/adapters/bluesky.py`; consumer:
`bsky_stream.py` under `news-radar-bsky.service`; gazetteer:
`scripts/load_gazetteer.py`. Spec: `specs/ingestion-expansion.md` R31.

## The source

- `wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post`,
  no auth. JSON per message: `{did, time_us, kind: "commit", commit:
  {operation: create|delete, collection, rkey, record: {text, langs, ...}}}`.
  `&cursor=<time_us>` resumes; Jetstream keeps roughly a day.
- Measured 2026-09-24: about 27 posts/s, roughly 2.2M a day, 1.9 GB/day of
  JSON. `langs` on 93% of posts: 49% en, 13% ja. Jetstream offers zstd, but
  it needs Jetstream's own dictionary; the consumer reads uncompressed.
- **Users keep the rights to their posts; there is no open-data licence.**
  So nothing of a post is stored except, for up to 20 posts per place per
  hour, its `at://` URI for 48 h, removed on a delete event. Display
  fetches the post live, so a deleted post simply fails to load.

## Matching, and where it goes wrong

Posts carry no location. Each post's text is matched against a GeoNames
gazetteer (cities over 15k people, first-level divisions, countries; about
370k names in many scripts) with an Aho-Corasick automaton. Rules:

- Case-sensitive ("Paris", not "paris"); word boundaries in any spaced
  script. Han names need 2 characters; kana and Thai 3, since a short
  transliteration sits inside ordinary words (ライ inside ライブ).
- All-caps alternates of 4 letters or fewer are airport or station codes
  ("BBC" was Bay City, "USA" was Concord) and are dropped.
- One place per name: country beats city beats division; among cities,
  the most populous (Paris, France over Paris, Texas).
- **`gazetteer_stop`** holds names that are words in the stream itself,
  measured by `bsky_stream.py --calibrate 900`: a name whose lowercase
  form is at least as common as its capitalized form ("THE", "But", "Der",
  and also the cities Nice, Split, Reading, Bath, Mobile).
- **Person names are not fixable this way**: Burnham, David, Chad, Jordan,
  Victoria. They are why counts are only meaningful against each place's
  own baseline, never as absolute volumes or between places.

About a quarter of posts match at least one place.

## The stored copy

| Table | Holds | Kept |
|---|---|---|
| `chatter_5min` | posts per place per 5 min (`source = 'bluesky'`), added by the consumer | 7 d |
| `chatter_hourly` | the same per hour, recomputed by `rollup.py` | 90 d |
| `bsky_uri` | up to 20 post URIs per place per hour | 48 h |
| `gazetteer_place`, `gazetteer_name` | GeoNames places (FIPS `country`, GDELT-style `adm1`) and all their names | monthly reload |
| `chatter_country_hourly` | view: posts per country per hour | |

```sql
-- Places Bluesky is talking about most in the last hour, vs their own last week.
WITH now_ AS (SELECT place_id, sum(posts) n FROM chatter_5min
              WHERE bucket > now() - interval '1 hour' GROUP BY 1),
     base AS (SELECT place_id, avg(posts) m FROM chatter_hourly
              WHERE hour > now() - interval '7 days' GROUP BY 1)
SELECT g.name, g.kind, g.country, n.n, round(b.m::numeric, 1) AS usual
FROM now_ n JOIN gazetteer_place g ON g.id = n.place_id LEFT JOIN base b USING (place_id)
ORDER BY n.n / greatest(b.m, 1) DESC LIMIT 20;

-- A few recent posts about a place (fetch each URI live to display it).
SELECT uri, at FROM bsky_uri WHERE place_id = 2988507 ORDER BY at DESC LIMIT 5;
```

Health: `source_health` row `bluesky-posts`; `journalctl --user -u news-radar-bsky`.
The gazetteer, country codes and stoplist refresh monthly via
`news-radar-monthly.timer`.
