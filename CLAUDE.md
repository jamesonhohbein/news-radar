# news-radar

Read `README.md` first, then `specs/news-radar.md`. This file holds only what
will bite an agent working here.

## Design rules that keep getting violated

- **The store is judgment-free.** No scoring, no interest profile, nothing
  dropped as unimportant. The agent (phase 3) is a lens applied on demand;
  the ingest path never asks "does this matter".
- **Acute detection is content-blind.** Region z-score and first-window
  `NumSources`. If you are about to put an LLM in the detector, stop.
- **This project is the one exception to the machine-wide one-incident-path
  rule**, decided 2026-09-20 and recorded in `~/.claude/CLAUDE.md`. It
  detects and delivers on its own, through sinks. Do not route it through
  Grafana or `alert_journal`, and do not cite it as precedent elsewhere.
- **Open source from day one.** Nothing host-specific goes in core: no
  authentik, no Home Assistant, no `/mnt/fast`. The homelab is `.env` and a
  gitignored compose override.

## Traps

**GDELT over https only.** The http host answers 301 and a non-following
client sees an empty body, which looks exactly like the service being down.

**Per-file commits are load-bearing in `ingest.py`.** `store.insert_events`
stages through a temp table with `ON COMMIT DROP`. Inside
`conn.transaction()` that would be a savepoint (a SELECT has already opened
the transaction), the drop never fires, and the second file dies with
"relation staging already exists". Commit explicitly.

**`added_at`, never `occurred_on`, for any time series.** GDELT's `Day` is
the date the article says the event happened; anniversaries and
retrospectives put events years in the past. `added_at` is when the source
saw it.

**Anomaly is materialized, not a view.** The 30-way same-hour self-join is
seconds per run in `rollup.py` and would be seconds per map load as a view.
It covers only the last 48 hours and is meaningless until 30 days are loaded.

**Port 5443 and a named volume.** 5436-5442 are other pipelines' databases
on this host. Docker's data root is on the NVMe here, so a named volume is
already off the shingled disk; do not bind-mount `/mnt/fast`, it is
root-owned.

**Tests need the container up.** They create `newsradar_test` on the running
server and drop it after. There is no in-process fallback.
