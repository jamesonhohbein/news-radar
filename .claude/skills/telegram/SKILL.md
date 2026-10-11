---
name: telegram
description: Read what state media outlets post on Telegram, directly from the public web preview or from news-radar's stored copy (tg_post, tg_channel, state_telegram_link). Use for anything about state media or propaganda channels on Telegram, what RT, TASS, RIA, Press TV, IRNA or other registry outlets are saying, a channel being unreachable, or adding a channel. Covers the preview endpoint and its markup, paging, why some channels answer 302, the content exception, and working SQL.
---

# State media Telegram channels

Code: `newsradar/adapters/telegram.py`, run by `scripts/collect_telegram.py`
from `news-radar-ingest.service` every 15 minutes. Spec:
`specs/state-telegram.md` R45 to R50. Channel list comes from the state
media registry (skill `state-media-monitor`).

## The source

- `https://t.me/s/<handle>` is Telegram's public web preview: no account,
  no key. 20 posts per page, oldest first, each in a
  `tgme_widget_message_wrap` with `data-post="<handle>/<id>"`,
  `<time datetime>`, `tgme_widget_message_text`, `tgme_widget_message_views`
  (`34.9K` style) and `tgme_widget_message_forwarded_from_name`.
- Older pages: `?before=<lowest id on the page>`. Post ids are a running
  counter per channel.
- A channel whose preview is off or restricted answers **302 to
  `https://t.me/<handle>`**. Measured 2026-10-11: `rt_russian` and `rtnews`.
  Those need the logged-in MTProto API, which this collector does not use.
  `tg_channel.preview = false`, retried once a day.
- Rates 2026-10-11: TASS about 20 posts an hour; RIA, Press TV, IRNA and
  BelTA a 20-post page every 2 to 6 hours.

## Which channels

`state_outlet_id` rows with `kind = 'telegram'`, valid usernames only, no
bots (`HANDLE` and the `bot` suffix in the adapter). Wikidata's P3789 misses
big channels; `rian_ru`, `presstv`, `irna_1313` come from
`data/state_media_ids.csv`. To add a channel, add a row there and run
`scripts/load_state_media.py`. Gazprom Media entertainment channels are
collected too; filter on outlet or typology when reading.

## Traps

- **This source stores text**, unlike the content-blind chatter sources
  (R31). Scope is registry channels only; never widen it to arbitrary
  channels without a spec.
- `media` holds kinds (photo, video, ...), not URLs: the CDN URLs are
  signed and expire.
- An edited post overwrites `text`; there is no edit history.
- `forwarded_from` is a channel handle when the source is public, else the
  display name.
- `links` includes `t.me` self-links; the link lens drops anything not in
  the registry.
- First run per channel backfills at most 7 days.

## The stored copy

`tg_channel(handle, outlet_id, title, subscribers, preview, checked_at)`,
`tg_post(handle, post_id, posted_at, text, views, forwarded_from, links,
media, first_seen, last_seen)`, view `state_telegram_link` (posts linking a
registered outlet's site). Health: `fetch_log` file `<handle>/<hour>` under
source `telegram-state`.

```sql
-- Last 24 h from state-controlled channels.
SELECT p.posted_at, p.handle, o.name, p.views, left(p.text, 200)
FROM tg_post p JOIN tg_channel c USING (handle) JOIN state_outlet o ON o.id = c.outlet_id
WHERE p.posted_at > now() - interval '1 day' AND o.typology = 'SC'
ORDER BY p.posted_at DESC;

-- Who amplifies whom: channel outlet -> linked outlet.
SELECT c.name AS channel_outlet, l.linked_outlet, count(*)
FROM state_telegram_link l JOIN state_outlet c ON c.id = l.channel_outlet_id
WHERE l.posted_at > now() - interval '7 days' AND l.channel_outlet_id <> l.linked_outlet_id
GROUP BY 1, 2 ORDER BY 3 DESC;
```

`scripts/load_state_media.py --report` lists channels with no preview or no
post in 7 days.
