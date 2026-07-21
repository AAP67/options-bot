#!/usr/bin/env bash
# Prove the migrations are reproducible from scratch (Sprint 1 exit criteria).
#
# Applies every supabase/migrations/*.sql in filename order to an EMPTY Postgres,
# then asserts the expected tables and indexes exist. Two modes:
#
#   DATABASE_URL set  -> uses the host `psql` against that database (CI: services.postgres)
#   DATABASE_URL unset -> spins a throwaway `postgres:16` container and tears it down
#
# Never point this at the real Supabase project: it assumes an empty database.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIGRATIONS_DIR="$REPO_ROOT/supabase/migrations"

EXPECTED_TABLES=(positions iv_history suggestions outcomes heartbeat sync_runs)
EXPECTED_INDEXES=(
  positions_synced_at_idx
  positions_symbol_idx
  iv_history_ticker_date_idx
  suggestions_run_id_idx
  suggestions_ticker_idx
  outcomes_suggestion_id_idx
  heartbeat_ran_at_idx
  sync_runs_synced_at_idx
  sync_runs_status_idx
)

CONTAINER=""
cleanup() {
  if [[ -n "$CONTAINER" ]]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# -- pick a runner: `psql <url> -c ...` or `docker exec` into a scratch container --

if [[ -n "${DATABASE_URL:-}" ]]; then
  run_sql()  { psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -c "$1"; }
  run_file() { psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -f "$1"; }
  query()    { psql "$DATABASE_URL" -tAc "$1"; }
else
  echo "==> DATABASE_URL unset; starting throwaway postgres:16 container"
  CONTAINER="$(docker run --rm -d -e POSTGRES_PASSWORD=postgres postgres:16)"
  for _ in $(seq 1 60); do
    if docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1; then break; fi
    sleep 1
  done
  docker exec "$CONTAINER" pg_isready -U postgres >/dev/null

  run_sql()  { docker exec -i "$CONTAINER" psql -U postgres -v ON_ERROR_STOP=1 -q -c "$1"; }
  run_file() { docker exec -i "$CONTAINER" psql -U postgres -v ON_ERROR_STOP=1 -q < "$1"; }
  query()    { docker exec -i "$CONTAINER" psql -U postgres -tAc "$1"; }
fi

# -- fail loudly if the target isn't empty -----------------------------------

existing="$(query "select count(*) from pg_tables where schemaname = 'public';" | tr -d '[:space:]')"
if [[ "$existing" != "0" ]]; then
  echo "ERROR: target database already has $existing public table(s); expected an empty database." >&2
  exit 1
fi

# -- apply in filename order --------------------------------------------------

shopt -s nullglob
migrations=("$MIGRATIONS_DIR"/*.sql)
if [[ ${#migrations[@]} -eq 0 ]]; then
  echo "ERROR: no migrations found in $MIGRATIONS_DIR" >&2
  exit 1
fi

for migration in "${migrations[@]}"; do
  echo "==> applying $(basename "$migration")"
  run_file "$migration"
done

# -- assert the resulting schema ---------------------------------------------

failed=0
for table in "${EXPECTED_TABLES[@]}"; do
  found="$(query "select to_regclass('public.$table') is not null;" | tr -d '[:space:]')"
  if [[ "$found" != "t" ]]; then
    echo "MISSING TABLE: $table" >&2
    failed=1
  fi
done

for index in "${EXPECTED_INDEXES[@]}"; do
  found="$(query "select count(*) from pg_indexes where schemaname='public' and indexname='$index';" | tr -d '[:space:]')"
  if [[ "$found" != "1" ]]; then
    echo "MISSING INDEX: $index" >&2
    failed=1
  fi
done

# Re-applying must be a no-op: every migration is `create ... if not exists`,
# so a replay on an already-migrated database must not error.
echo "==> replaying migrations (idempotency check)"
for migration in "${migrations[@]}"; do
  run_file "$migration"
done

if [[ "$failed" -ne 0 ]]; then
  echo "FAILED: schema does not match expectations" >&2
  exit 1
fi

echo "OK: ${#migrations[@]} migration(s) applied from scratch; ${#EXPECTED_TABLES[@]} tables, ${#EXPECTED_INDEXES[@]} indexes verified; replay clean."
