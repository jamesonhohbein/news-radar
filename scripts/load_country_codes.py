#!/usr/bin/env python3
"""Load ISO2/ISO3/FIPS country codes from GeoNames countryInfo.txt (CC BY 4.0)
into country_code. Idempotent; run once, and again if GeoNames changes."""
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from newsradar.db import connect  # noqa: E402

URL = "https://download.geonames.org/export/dump/countryInfo.txt"


def main() -> None:
    with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "news-radar/0.1"}), timeout=60) as r:
        lines = r.read().decode("utf-8").splitlines()
    rows = [l.split("\t") for l in lines if l and not l.startswith("#")]
    with connect() as conn:
        for f in rows:
            conn.execute("""INSERT INTO country_code (iso2, iso3, fips, name) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (iso2) DO UPDATE SET iso3 = EXCLUDED.iso3, fips = EXCLUDED.fips, name = EXCLUDED.name""",
                         (f[0], f[1], f[3] or None, f[4]))
        conn.commit()
    print(f"country_code: {len(rows)} rows")


if __name__ == "__main__":
    main()
