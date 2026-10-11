#!/usr/bin/env python3
"""Collect new posts from the state media registry's Telegram channels
(specs/state-telegram.md). Run every 15 minutes by news-radar-ingest.service.
Exits non-zero when any channel failed, after collecting the rest."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from newsradar.adapters import telegram  # noqa: E402
from newsradar.db import connect  # noqa: E402


def main() -> None:
    with connect() as conn:
        errors = telegram.run(conn)
    for e in errors:
        print(e, file=sys.stderr)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
