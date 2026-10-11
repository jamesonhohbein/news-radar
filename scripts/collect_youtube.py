#!/usr/bin/env python3
"""Collect new uploads from the state media registry's YouTube channels
through Invidious (specs/state-youtube.md). Run every 15 minutes by
news-radar-ingest.service; each channel is fetched at most hourly. Exits
non-zero when any channel failed or Invidious throttled the run."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from newsradar.adapters import youtube  # noqa: E402
from newsradar.db import connect  # noqa: E402


def main() -> None:
    if not youtube.base_url():
        print("NEWSRADAR_INVIDIOUS_URL is not set; YouTube collection skipped", file=sys.stderr)
        return
    with connect() as conn:
        errors = youtube.run(conn)
    for e in errors:
        print(e, file=sys.stderr)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
