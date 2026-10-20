#!/usr/bin/env bash
# Give the space freed by an archive pass back to the filesystem (specs/archive.md,
# Decision 8, R67). Plain VACUUM only lets Postgres reuse it; VACUUM FULL rewrites
# the table, which takes an exclusive lock for minutes, so the news-radar timers
# are stopped first and restarted on any exit.
#
#   scripts/reclaim.sh                     # the tables the first pass shrank
#   scripts/reclaim.sh event theme_daily   # any others
#
# Runs psql inside the container with the container's own POSTGRES_USER and
# POSTGRES_DB, so no credential passes through this script.
set -euo pipefail

CONTAINER=${NEWSRADAR_DB_CONTAINER:-news-radar-db}
TABLES=("$@")
[ ${#TABLES[@]} -gt 0 ] || TABLES=(theme_hourly mention_hourly attention_hourly)
for t in "${TABLES[@]}"; do
    [[ $t =~ ^[a-z_]+$ ]] || { echo "bad table name: $t" >&2; exit 2; }
done

psql_c() {
    docker exec -i "$CONTAINER" sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' psql "$@"
}

sizes() {
    local list
    list=$(printf "'%s'," "${TABLES[@]}")
    psql_c -A -F $'\t' -t -c "SELECT relname, pg_size_pretty(pg_total_relation_size(oid)), pg_total_relation_size(oid)
                               FROM pg_class WHERE relname IN (${list%,}) AND relkind = 'r' ORDER BY relname"
    psql_c -A -t -c "SELECT 'database', pg_size_pretty(pg_database_size(current_database()))"
}

mapfile -t TIMERS < <(systemctl --user list-units 'news-radar-*.timer' --state=active --no-legend --plain | awk '{print $1}')
restart() {
    if [ ${#TIMERS[@]} -gt 0 ]; then
        systemctl --user start "${TIMERS[@]}"
        echo "timers restarted: ${TIMERS[*]}"
    fi
}
trap restart EXIT

if [ ${#TIMERS[@]} -gt 0 ]; then
    systemctl --user stop "${TIMERS[@]}"
    echo "timers stopped: ${TIMERS[*]}"
fi
# Let a run already in flight finish rather than kill it mid-file.
for svc in news-radar-ingest news-radar-fast news-radar-slow news-radar-rollup news-radar-monthly; do
    while systemctl --user is-active --quiet "$svc.service"; do
        echo "waiting for $svc.service to finish"
        sleep 10
    done
done

echo "== before"
sizes
for t in "${TABLES[@]}"; do
    echo "VACUUM FULL $t"
    start=$(date +%s)
    psql_c -c "VACUUM (FULL, ANALYZE) $t"
    echo "  $(( $(date +%s) - start )) s"
done
echo "== after"
sizes
