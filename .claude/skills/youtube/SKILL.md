---
name: youtube
description: Read what state media outlets upload to YouTube, from news-radar's stored copy (yt_video, yt_channel, state_youtube_link) or live through the Invidious API. Use for anything about state media or broadcaster channels on YouTube, what Al Jazeera, Yonhap, CGTN, PTV or other registry outlets are uploading, a channel YouTube has terminated, or adding a channel. Covers why the source is Invidious and not YouTube's feed, the two endpoints, publish-time precision, the content exception, and working SQL.
---

# State media YouTube channels

Code: `newsradar/adapters/youtube.py`, run by `scripts/collect_youtube.py`
from `news-radar-ingest.service` every 15 minutes; each channel is fetched
at most every 55 minutes. Spec: `specs/state-youtube.md` R51 to R56.
Channel list comes from the state media registry (skill
`state-media-monitor`).

## The source

- **YouTube's keyless feed is dead here.** `/feeds/videos.xml?channel_id=`
  answered 404 or 500 for every registry channel and for Google's own on
  2026-10-11; feed readers report it flaky since December 2025. Do not
  "simplify" back to it.
- **Invidious** (InnerTube behind it, no key). `NEWSRADAR_INVIDIOUS_URL`
  (`.env` or environment) names the instance; on hog it is
  `http://127.0.0.1:3088`, the household instance behind yt.jameson.casa,
  set by the user drop-in `news-radar-ingest.service.d/invidious.conf`. Unset, the collector does
  nothing.
- `GET /api/v1/channels/<UC id>/videos`: the 60 latest uploads, with
  `videoId`, `title`, `author`, `published` (resolved from "12 minutes
  ago", so precise for new uploads and coarse for old ones), `viewCount`,
  `lengthSeconds`, `liveNow`, `isUpcoming`. `description` is empty here.
- `GET /api/v1/videos/<id>`: `description`, `keywords`, `subCountText`.
  Its `published` is the **date at midnight**; never prefer it over the
  list's time.
- A taken-down channel answers 500 with `{"error": "This account has been
  terminated ..."}` or similar ("removed", "not available", "does not
  exist"). Measured 2026-10-11: 12 of 97 gone, RT's among them.
  `yt_channel.status = 'gone'`, retried weekly.
- A channel can 302 to a different channel id (FMM's
  `UCdTyuXgmJkG_O8_75eqej-w`); urllib follows it.
- Load: about 2,000 list and 1,200 detail calls a day. The household
  shares the instance, so a 429 or 403 ends the run at once.

## Which channels

`state_outlet_id` rows with `kind = 'youtube'` that are `UC...` ids. To
add one, add a row to `data/state_media_ids.csv` and run
`scripts/load_state_media.py`.

## Traps

- **This source stores text** (titles, descriptions), like Telegram and
  unlike the content-blind chatter sources (R31). Registry channels only.
- The first sight of a channel stores its 60 latest uploads; there is no
  deeper backfill.
- Busy channels (Yonhap, PTV, Al Jazeera) upload about 100 a day; 60
  entries cover about 13 hours, so a stall longer than that loses videos.
- No transcripts, thumbnails or comments.

## The stored copy

`yt_channel(channel_id, outlet_id, title, subscribers, status, reason,
checked_at)`, `yt_video(video_id, channel_id, published_at, title,
description, keywords, length_s, views, live, first_seen, last_seen)`, view
`state_youtube_link` (description links to a registered outlet's site).
Health: `fetch_log` file `<channel_id>/<hour>` under source
`youtube-state`; `throttled/<hour>` when Invidious pushed back.

```sql
-- Last 24 h of uploads from state-controlled channels.
SELECT v.published_at, o.name, v.views, v.title
FROM yt_video v JOIN yt_channel c USING (channel_id) JOIN state_outlet o ON o.id = c.outlet_id
WHERE v.published_at > now() - interval '1 day' AND o.typology = 'SC'
ORDER BY v.published_at DESC;

-- Same story on YouTube and Telegram: titles sharing a keyword in a day.
SELECT o.name, v.title FROM yt_video v JOIN yt_channel c USING (channel_id)
JOIN state_outlet o ON o.id = c.outlet_id
WHERE v.published_at > now() - interval '1 day' AND v.title ILIKE '%Lebanon%';
```

`scripts/load_state_media.py --report` lists gone channels and live ones
with no upload in 7 days.
