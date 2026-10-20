#!/usr/bin/env python3
"""Query the Parquet archive (specs/archive.md).

    archive.py query "SELECT count(*) FROM theme_hourly WHERE theme = 'TAX'"

Each archived table is a DuckDB view named like its Postgres table, over
NEWSRADAR_ARCHIVE_DIR/<table>/*/*.parquet, so a lens query runs unchanged
except that geometry is lon/lat columns (geom_lon/geom_lat on
firms_detection) and jsonb is JSON text. Output is tab-separated with a
header. Tables with no archived files yet have no view.
"""
from __future__ import annotations

import argparse
import sys

from newsradar import archive
from newsradar.db import load_env


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("query", help="run SQL against the archive")
    q.add_argument("sql")
    q.add_argument("--dir", help="archive dir (default NEWSRADAR_ARCHIVE_DIR)")
    args = ap.parse_args(argv)
    root = args.dir or load_env().get("NEWSRADAR_ARCHIVE_DIR")
    if not root:
        sys.exit("NEWSRADAR_ARCHIVE_DIR is not set; pass --dir")
    cols, rows = archive.query(args.sql, root)
    print("\t".join(cols))
    for r in rows:
        print("\t".join("" if v is None else str(v) for v in r))


if __name__ == "__main__":
    main()
