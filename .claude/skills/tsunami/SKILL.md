---
name: tsunami
description: Access US tsunami warning centre bulletins (NTWC Palmer and PTWC Honolulu) directly or through news-radar's stored copy. Use for anything about tsunami warnings, watches, advisories or information statements, whether a quake poses a tsunami threat to the US West Coast, BC, Alaska, Hawaii or the Pacific, or the tsunami layer on the news-radar map. Covers the two Atom feeds, why history exists only by polling, where the category hides, and working SQL.
---

# Tsunami bulletins

Adapter: `newsradar/adapters/tsunami.py`. Spec: `specs/ingestion-expansion.md` R25.

## The source

- Feeds, both Atom with `geo:lat`/`geo:long`, public domain, no key:
  - `https://www.tsunami.gov/events/xml/PAAQAtom.xml`: National Tsunami
    Warning Center, Palmer AK. Covers the US West Coast (including
    Bellingham), British Columbia and Alaska.
  - `https://www.tsunami.gov/events/xml/PHEBAtom.xml`: Pacific Tsunami
    Warning Center, Honolulu. Hawaii, US Pacific and Caribbean territories,
    and international Pacific guidance.
- About 2 KB each. **Each holds only the latest bulletin.** History exists
  only because the adapter polls every 15 minutes and keeps every new entry
  id (`urn:uuid`, one per bulletin). A bulletin issued and superseded
  between two polls is missed; for those, the per-bulletin CAP and text
  links under `https://www.tsunami.gov/events/<centre>/...` are the record.
- Volume: a few bulletins a month, most of them Information statements.

## Traps

- **The category is only in the summary's XHTML**
  (`<strong>Category:</strong> Information`), as is the preliminary
  magnitude (`6.3(Mwp)`). The adapter reads both with a regex that allows
  a namespace prefix on the tags.
- Categories, lowest to highest: Information (an earthquake happened, no
  threat), Advisory, Watch, Warning, Threat (international guidance).
- The feed-level `<title>` is the bulletin type and number; the entry
  `<title>` is the region.

## The stored copy

`event` rows with source `tsunami-bulletins`, `attention = false`. `props`:
`centre`, `issuer`, `category`, `title`, `magnitude`, `magnitude_text`,
`cap` (the CAP XML URL). `url` is the plain-text bulletin.

```sql
SELECT added_at, props->>'centre' AS centre, props->>'category' AS category,
       props->>'magnitude' AS mag, geo_name
FROM event e JOIN source s ON s.id = e.source_id AND s.name = 'tsunami-bulletins'
ORDER BY added_at DESC LIMIT 10;
```

The map (`primary_live`) draws bulletins above Information from the last
24 h. Fetched every 15 min by `ingest.py primary`.
