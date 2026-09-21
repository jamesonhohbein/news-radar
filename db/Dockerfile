# PostGIS image plus pgvector. postgis/postgis is Debian with the PGDG repo
# already configured, so pgvector is one apt package; the pgvector/pgvector
# image has no PostGIS and building PostGIS from source is the worse trade.
FROM postgis/postgis:17-3.5
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-pgvector \
 && rm -rf /var/lib/apt/lists/*
