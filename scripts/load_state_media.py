#!/usr/bin/env python3
"""Load the state media registry (specs/state-media.md): outlets and typology
from State Media Monitor, identifiers from Wikidata and data/state_media_ids.csv.
Idempotent; run monthly. --report lists the coverage gaps (R43)."""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from newsradar import statemedia as sm  # noqa: E402
from newsradar.db import connect  # noqa: E402


def print_report(conn) -> None:
    r = sm.report(conn)
    print(f"# Outlets with no host ({len(r['no_host'])})")
    for t, c, n in r["no_host"]:
        print(f"{t}\t{c}\t{n}")
    print(f"\n# Hosts with no GKG article in 7 days ({len(r['silent_host'])})")
    for t, c, n, h in r["silent_host"]:
        print(f"{t}\t{c}\t{n}\t{h}")
    print(f"\n# Telegram channels with no preview or no post in 7 days ({len(r['telegram'])})")
    for t, c, n, h, why in r["telegram"]:
        print(f"{why}\t{t}\t{c}\t{n}\t{h}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="print coverage gaps and exit")
    args = ap.parse_args()
    with connect() as conn:
        if args.report:
            print_report(conn)
            return
        outlets = sm.outlets_from_posts(sm.fetch_posts())
        new = sm.upsert_outlets(conn, outlets, date.today())
        conn.commit()
        wd = sm.Wikidata()
        iso2 = sm.iso2_map(conn)
        rows = conn.execute("SELECT id, country, name, typology, trajectory, smm_url FROM state_outlet").fetchall()
        matches = {}
        for oid, country, name, typ, traj, url in rows:
            qid, ids = sm.resolve(wd, sm.Outlet(country, name, typ, traj, url), iso2.get(country.casefold()))
            if qid:
                matches[oid] = (qid, ids)
        stored = sm.store_wikidata(conn, matches)
        manual = sm.load_manual(conn)
        conn.commit()
    print(f"state_outlet: {len(outlets)} from SMM, {new} new; wikidata: {len(matches)}/{len(rows)} matched, "
          f"{stored} identifiers; manual: {manual}")


if __name__ == "__main__":
    main()
