#!/usr/bin/env bash
# Run EXPLAIN (ANALYZE, BUFFERS) for a query file and save the plan.
#
# Usage: scripts/explain_query.sh <query.sql> <output.txt> [psql -v args...]
#   e.g. scripts/explain_query.sh sql/10_a1_rolling_features_naive.sql evidence/sql/A1_before_explain.txt
# The query file must contain a single statement; psql meta-commands (\if, \set, SET) are kept as-is.
set -euo pipefail

query_file="$1"
out_file="$2"
shift 2

container="${PG_CONTAINER:-ds_interview_postgres}"
pg_user="${PG_USER:-ds_candidate}"
pg_db="${PG_DB:-ecommerce}"

mkdir -p "$(dirname "$out_file")"

# Insert EXPLAIN before the first line that starts the statement (WITH or SELECT).
explain_sql=$(awk '!done && /^(WITH|SELECT)/ { print "EXPLAIN (ANALYZE, BUFFERS)"; done=1 } { print }' "$query_file")

{
  echo "-- query: $query_file"
  echo "-- captured: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "$explain_sql" | docker exec -i "$container" psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 "$@"
} > "$out_file"

grep -E "Execution Time|Planning Time" "$out_file" | sed "s|^|$(basename "$query_file"): |"
