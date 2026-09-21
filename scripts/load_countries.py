#!/usr/bin/env python3
"""Load web/public/countries.geojson into country_shape for reverse geocoding.
Two features can share a FIPS code (Cyprus, Somalia); they are unioned. Run once, and again if the file changes."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from newsradar.db import connect  # noqa: E402

geo = json.loads((Path(__file__).resolve().parent.parent / "web/public/countries.geojson").read_text())
with connect() as conn:
    for f in geo["features"]:
        conn.execute("""INSERT INTO country_shape (fips, name, geom)
                        VALUES (%s, %s, ST_Multi(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))))
                        ON CONFLICT (fips) DO UPDATE SET geom = ST_Multi(ST_CollectionExtract(ST_Union(country_shape.geom, EXCLUDED.geom), 3))""",
                     (f["properties"]["fips"], f["properties"]["name"], json.dumps(f["geometry"])))
    conn.commit()
    print(conn.execute("SELECT count(*) FROM country_shape").fetchone()[0], "countries loaded")
