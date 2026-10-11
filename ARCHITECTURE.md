# Architecture

Updated to `specs/state-media.md` After. Earlier specs predate the diagram
policy; the adapters box stands for every source under `newsradar/`.

```mermaid
flowchart LR
  GDELT[GDELT GKG] --> GKG[(gkg_article)]
  Other[USGS, GDACS, UCDP, ... adapters] --> Events[(event tables)]
  SMM[State Media Monitor wp-json] --> Loader[load_state_media.py monthly]
  WD[Wikidata API] --> Loader
  CSV[data/state_media_ids.csv] --> Loader
  Loader --> SO[(state_outlet)]
  Loader --> SOI[(state_outlet_id)]
  GKG --> SMA[[state_media_article view<br/>host_suffixes join]]
  SOI --> SMA
  SO --> SMA
  GKG --> Lens[lens and detector queries]
  Events --> Lens
  SMA --> Lens
```
