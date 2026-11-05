#!/usr/bin/env bash
# Prove two query files return the same multiset of rows (two-way EXCEPT ALL).
#
# Usage: scripts/check_equivalence.sh <naive.sql> <optimized.sql> [psql -v args...]
# Prints row counts of both queries and of each EXCEPT ALL direction.
# Exits 1 if either direction is non-empty or the row counts differ.
# EXCEPT ALL (not EXCEPT) is used so duplicate rows count: events has no primary key.
set -euo pipefail

naive_file="$1"
opt_file="$2"
shift 2

container="${PG_CONTAINER:-ds_interview_postgres}"
pg_user="${PG_USER:-ds_candidate}"
pg_db="${PG_DB:-ecommerce}"

meta_lines() { grep -E '^\\' "$1" || true; }
# Query body without psql meta-commands, comments-only lines and the trailing semicolon.
query_body() { grep -vE '^\\|^--' "$1" | sed -e 's/;[[:space:]]*$//'; }

naive_sql=$(query_body "$naive_file")
opt_sql=$(query_body "$opt_file")

sql=$(cat <<EOF
$(meta_lines "$naive_file")
SELECT
    (SELECT COUNT(*) FROM ($naive_sql) n) AS naive_rows,
    (SELECT COUNT(*) FROM ($opt_sql) o)   AS optimized_rows,
    (SELECT COUNT(*) FROM (($naive_sql) EXCEPT ALL ($opt_sql)) d) AS naive_except_optimized,
    (SELECT COUNT(*) FROM (($opt_sql) EXCEPT ALL ($naive_sql)) d) AS optimized_except_naive;
EOF
)

result=$(echo "$sql" | docker exec -i "$container" psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 -At -F ' ' "$@")
read -r naive_rows opt_rows n_minus_o o_minus_n <<< "$result"

echo "$(basename "$naive_file") vs $(basename "$opt_file"): naive_rows=$naive_rows optimized_rows=$opt_rows naive_except_optimized=$n_minus_o optimized_except_naive=$o_minus_n"

if [[ "$naive_rows" != "$opt_rows" || "$n_minus_o" != "0" || "$o_minus_n" != "0" ]]; then
  echo "RESULT: DIFFERENT" >&2
  exit 1
fi
echo "RESULT: IDENTICAL"
