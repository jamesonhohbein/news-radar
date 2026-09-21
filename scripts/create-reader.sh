#!/usr/bin/env bash
# Create (or re-password) the read-only `reader` role the web app and the
# agent connect as. READER_PASSWORD comes from .env; the role gets SELECT on
# everything now and in future, and a 10 s statement timeout so a bad query
# from the agent cannot hold the database.
set -euo pipefail
cd "$(dirname "$0")/.."
pw=$(grep -E '^READER_PASSWORD=' .env | cut -d= -f2-)
[ -n "$pw" ] || { echo "READER_PASSWORD missing from .env" >&2; exit 1; }
docker exec -i -e PW="$pw" news-radar-db psql -U newsradar -d newsradar -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reader') THEN
    CREATE ROLE reader LOGIN;
  END IF;
END $$;
\set pw `echo "$PW"`
ALTER ROLE reader PASSWORD :'pw';
ALTER ROLE reader SET statement_timeout = '10s';
GRANT CONNECT ON DATABASE newsradar TO reader;
GRANT USAGE ON SCHEMA public TO reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO reader;
ALTER DEFAULT PRIVILEGES FOR ROLE newsradar IN SCHEMA public GRANT SELECT ON TABLES TO reader;
SQL
echo "reader role ready"
