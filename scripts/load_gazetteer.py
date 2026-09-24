#!/usr/bin/env python3
"""Load the gazetteer (R31) from GeoNames: cities15000, admin1 codes and
countryInfo (CC BY 4.0). Replaces gazetteer_place and gazetteer_name in one
transaction. Run monthly (news-radar-monthly.timer), after
load_country_codes.py. Downloads go to a temp dir, about 4 MB."""
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from newsradar.adapters.bluesky import gazetteer_rows  # noqa: E402
from newsradar.db import connect  # noqa: E402

BASE = "https://download.geonames.org/export/dump/"


def _get(name: str) -> bytes:
    req = urllib.request.Request(BASE + name, headers={"User-Agent": "news-radar/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main() -> None:
    cities = zipfile.ZipFile(io.BytesIO(_get("cities15000.zip"))).read("cities15000.txt").decode("utf-8").splitlines()
    admin1 = _get("admin1CodesASCII.txt").decode("utf-8").splitlines()
    info = [l.split("\t") for l in _get("countryInfo.txt").decode("utf-8").splitlines() if l and not l.startswith("#")]
    rows = list(gazetteer_rows(cities, admin1, [(f[0], f[4], f[16]) for f in info]))
    with connect() as conn:
        conn.execute("TRUNCATE gazetteer_place CASCADE")
        with conn.cursor() as cur:
            with cur.copy("COPY gazetteer_place (id, name, kind, iso2, adm1, geom, population) FROM STDIN") as copy:
                seen = set()
                for pid, name, kind, iso2, adm1, lat, lon, pop, _ in rows:
                    if pid in seen:
                        continue
                    seen.add(pid)
                    copy.write_row((pid, name, kind, iso2, adm1,
                                    f"SRID=4326;POINT({lon} {lat})" if lat is not None else None, pop))
            with cur.copy("COPY gazetteer_name (place_id, name) FROM STDIN") as copy:
                done = set()
                for pid, *_rest, names in rows:
                    for n in names:
                        if (pid, n) not in done:
                            done.add((pid, n))
                            copy.write_row((pid, n))
        # FIPS country; GDELT-style ADM1 (FIPS country + admin1 code).
        conn.execute("""UPDATE gazetteer_place g SET country = c.fips,
                               adm1 = CASE WHEN g.adm1 IS NOT NULL AND g.adm1 <> '' AND c.fips IS NOT NULL
                                           THEN c.fips || g.adm1 END
                        FROM country_code c WHERE c.iso2 = g.iso2""")
        # A division sits at the population-weighted centre of its cities;
        # a country at a point on its shape.
        conn.execute("""UPDATE gazetteer_place a SET geom = x.p FROM (
                            SELECT country, adm1,
                                   ST_SetSRID(ST_MakePoint(sum(ST_X(geom) * greatest(population, 1)) / sum(greatest(population, 1)),
                                                           sum(ST_Y(geom) * greatest(population, 1)) / sum(greatest(population, 1))), 4326) p
                            FROM gazetteer_place WHERE kind = 'city' GROUP BY 1, 2) x
                        WHERE a.kind = 'adm1' AND a.country = x.country AND a.adm1 = x.adm1""")
        conn.execute("""UPDATE gazetteer_place g SET geom = ST_PointOnSurface(s.geom)
                        FROM country_shape s WHERE g.kind = 'country' AND s.fips = g.country""")
        n = conn.execute("SELECT kind, count(*), count(geom) FROM gazetteer_place GROUP BY 1 ORDER BY 1").fetchall()
        names = conn.execute("SELECT count(*) FROM gazetteer_name").fetchone()[0]
        conn.commit()
    print(f"gazetteer: {n}, {names} names")


if __name__ == "__main__":
    main()
