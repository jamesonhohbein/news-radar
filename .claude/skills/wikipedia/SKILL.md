---
name: wikipedia
description: Access Wikipedia edit activity (Wikimedia EventStreams plus the GeoData API) directly or through news-radar's stored copy. Use for anything about which Wikipedia pages are being edited heavily right now, new articles being created about an event, geotagged pages bursting in a country, or debugging the news-radar-wiki consumer. Covers the SSE stream and resume, why most of it is discarded, how pages get coordinates, the privacy rule on usernames, and working SQL.
---

# Wikipedia edits

Adapter: `newsradar/adapters/wikipedia.py`; consumer: `wiki_stream.py`
under `news-radar-wiki.service`. Spec: `specs/ingestion-expansion.md` R30.

## The source

- Stream: `https://stream.wikimedia.org/v2/stream/recentchange`,
  server-sent events, no key. Send a User-Agent. Each event carries an
  `id:` line (a JSON list of partition offsets); sending it back as
  `Last-Event-ID` resumes the stream where it stopped.
- **No server-side filter.** Every Wikimedia project arrives (Wikidata and
  Commons dominate); the consumer keeps `namespace == 0`,
  `type in (edit, new)`, `server_name` ending `.wikipedia.org`. Measured
  2026-09-24: ~4.4 kept edits/s across ~50 wikis, 44% by bots. Page
  creations are type `new` here, so the separate `page-create` stream is
  not needed.
- Coordinates: `https://<server>/w/api.php?action=query&prop=coordinates&coprimary=primary&titles=A|B&format=json&formatversion=2&redirects=1`,
  up to 50 titles per call per wiki. Most pages have none (2 of 18 in the
  probe); the ones that do are places and events.
- Content is CC BY-SA. **None is stored**, and usernames are stored only as
  a 12-character SHA-256 prefix, used for distinct-editor counts.

## How it runs

The consumer flushes every 10 s: rows into `wiki_edit` (keyed
`(wiki, rev)`, so a replay after resume inserts nothing) and the cursor
into `stream_cursor`, in one transaction. Every 60 s it geolocates hot
pages: 3+ distinct human editors in the last 2 h, or created by a human,
not cached or checked over 7 days ago. `rollup.py` rolls the buffer into
`wiki_activity` hourly.

## The stored copy

| Table | Holds | Kept |
|---|---|---|
| `wiki_edit` | wiki, title, time, user hash, bot, created, rev | 72 h |
| `wiki_activity` | per page per hour: edits, distinct human editors, created | 90 d |
| `wiki_page` | coordinates and country for pages that got busy (NULL geom = none) | |
| `wiki_geo_hourly` | view: geotagged pages' editors per country per hour | |

```sql
-- Busiest pages in the last hour, any wiki.
SELECT wiki, title, count(DISTINCT user_hash) FILTER (WHERE NOT bot) AS humans, count(*) AS edits
FROM wiki_edit WHERE at > now() - interval '1 hour'
GROUP BY 1, 2 ORDER BY humans DESC LIMIT 20;

-- New geotagged articles today.
SELECT a.wiki, a.title, p.country, ST_Y(p.geom), ST_X(p.geom)
FROM wiki_activity a JOIN wiki_page p USING (wiki, title)
WHERE a.created AND p.geom IS NOT NULL AND a.hour > now() - interval '1 day';
```

Health: `source_health` row `wikipedia-edits` (expects rows hourly);
`journalctl --user -u news-radar-wiki`.
