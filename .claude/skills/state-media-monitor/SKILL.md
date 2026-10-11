---
name: state-media-monitor
description: Access the state media registry (which outlets a government runs, their State Media Monitor typology, and their hostnames and Telegram, YouTube and X handles) directly or through news-radar's stored copy. Use for anything about state media, state-controlled or captured outlets, propaganda channels, whether a site or channel is government-run, or what state outlets are covering in GDELT. Covers where SMM's data actually lives (no bulk file), the typology codes, how Wikidata identifiers are matched and why many outlets have none, host suffix matching, the manual CSV, and working SQL.
---

# State Media Monitor registry

Code: `newsradar/statemedia.py`, loader `scripts/load_state_media.py`.
Spec: `specs/state-media.md` R39 to R44.

## The sources

- **State Media Monitor** (Media and Journalism Research Center, CC BY 4.0,
  doi:10.5281/zenodo.17219015). The Zenodo record holds only a README; the
  data is the WordPress site, read through
  `https://statemediamonitor.com/wp-json/wp/v2/posts` (664 posts on
  2026-10-11, 7 pages of 100).
  - Three regional pages (Eurasia, Asia, MENA) embed `const DATA = [...]`
    in the post body: region, country, outlet, assets, and `t`, the
    typology for 2022..2026 (null where unrated). 249 outlets.
  - Newer profile pages carry a Quick Facts card,
    `<span>Spain &middot; Quick Facts</span>` then the code in the next
    div, plus a "Typology trajectory" card. Subnational broadcasters read
    `Spain &middot; Catalonia &middot; Quick Facts`; the second part goes
    in `region`.
  - Europe, Africa and the Americas are mostly prose overviews; an outlet
    with no profile page is not in the registry. KCNA is absent entirely.
  - The same outlet appears under different names in DATA and its profile
    (`Xinhua (New China News Agency)` vs `Xinhua`). `name_keys()` dedupes
    on normalised name variants and acronyms, DATA winning. Pairs with no
    shared variant survive as two rows (`China Daily` and `China Daily
    Group`, `Channel One` and `Channel One Russia`).
- **Typology codes**, most to least state-run: SC state-controlled; CaPu
  captured public (nominally independent body, government-run); CaPr
  captured private; ISFM, ISM, ISF, IP editorially independent public
  media (BBC-like). Every code is stored; "state media" for the lens is
  usually `typology IN ('SC', 'CaPu', 'CaPr')`.
- **Wikidata** (CC0), `https://www.wikidata.org/w/api.php`:
  `wbsearchentities` on each name variant (limit 15), then
  `wbgetentities`. Identifiers: P856 website, P3789 Telegram, P2397
  YouTube channel id, P2002 X, from the item and its P355 subsidiaries.

## Matching rules and why

- An item counts only if its P17 country has ISO code P297 equal to the
  outlet's (via `country_code`, with `SMM_ISO2` for SMM's own spellings)
  or its label/alias is SMM's country name. Name alone fails: "China" is
  not an English alias of Q148.
- Several in-country items narrow to those whose label or alias equals a
  name variant exactly (IRIB, not IRIB TV1). Still several, or none: no
  match. A wrong match is worse than a missing one.
- First load 2026-10-11: 188 of 310 outlets matched, 655 identifiers.
  With a host: SC 100/183, CaPu 30/56, CaPr 9/23.
- Misses are mostly agencies whose search hits a namesake first (Xinhua,
  China Daily, Rossiya Segodnya). They go in `data/state_media_ids.csv`.

## Traps

- **P355 subsidiaries can be wrong.** Radio France picked up `rfi.fr`,
  which belongs to France Medias Monde. Wrong subsidiaries so far are on
  independent public broadcasters; check the state ones after a refresh.
- **GDELT misses most Russian and Iranian state output.** Week to
  2026-10-11: news.cn 1158, chinadaily.com.cn 1037, sputnikglobe.com 144,
  but rt.com, tass.ru, presstv.ir and irna.ir near zero. A host in the
  "no GKG article" section of the report is a collection gap, not a quiet
  outlet.
- `gkg_article.site` is the full hostname, so matching is by label
  suffix (`host_suffixes()` in `schema.sql`), never `LIKE '%host'`
  (`fakenews.cn` must not match `news.cn`).
- A Wikidata refresh deletes and rewrites every `origin = 'wikidata'` row.
  Manual rows are never touched and win any clash; a manual row naming an
  outlet not in the registry aborts the load.
- An outlet dropped by SMM keeps its row; `last_seen` goes stale.
- A full load makes about 1,000 Wikidata calls and takes around 10 minutes.

## The stored copy

`state_outlet` (one row per outlet, `typology`, `trajectory` 2022..2026),
`state_outlet_id` (`kind` host|telegram|youtube|x, `origin`
wikidata|manual, `qid`), view `state_media_article` (GKG article joined to
its outlet). Refreshed by `news-radar-monthly.timer`.

```sql
-- What state outlets covered in the last 7 days, by theme.
SELECT t.theme, count(*) AS articles, count(DISTINCT a.outlet) AS outlets
FROM state_media_article a, unnest(a.themes) AS t(theme)
WHERE a.added_at > now() - interval '7 days' AND a.typology IN ('SC', 'CaPu', 'CaPr')
GROUP BY 1 ORDER BY 2 DESC LIMIT 30;

-- Telegram channels of state-controlled outlets.
SELECT o.country, o.name, i.value FROM state_outlet o
JOIN state_outlet_id i ON i.outlet_id = o.id AND i.kind = 'telegram'
WHERE o.typology = 'SC' ORDER BY 1, 2;
```

`scripts/load_state_media.py --report` lists outlets with no host and hosts
with no GKG article in 7 days.
