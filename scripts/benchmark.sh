#!/usr/bin/env bash
# Benchmark query files: median server execution time of N runs, cold and warm cache.
#
# Usage: scripts/benchmark.sh <label> <query.sql>... [-- psql -v args]
#   label: name of the result set, e.g. "before" or "after"
# Output: evidence/sql/benchmark_<label>.csv  (query,cache,run1..runN,median_ms)
#
# Timing = "Execution Time" from EXPLAIN (ANALYZE, TIMING OFF): server-side work only,
# excluding client transfer of the result rows.
# cold = Postgres restarted (empty shared_buffers) and the Docker VM page cache dropped
#        before every run.
# warm = one discarded warm-up run, then N runs back to back.
set -euo pipefail

label="$1"; shift
queries=()
while [[ $# -gt 0 && "$1" != "--" ]]; do queries+=("$1"); shift; done
[[ "${1:-}" == "--" ]] && shift
psql_args=("$@")

runs="${RUNS:-5}"
container="${PG_CONTAINER:-ds_interview_postgres}"
pg_user="${PG_USER:-ds_candidate}"
pg_db="${PG_DB:-ecommerce}"
out_file="evidence/sql/benchmark_${label}.csv"
mkdir -p "$(dirname "$out_file")"

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }

run_once() {
  local query_file="$1"
  awk '!done && /^(WITH|SELECT)/ { print "EXPLAIN (ANALYZE, TIMING OFF)"; done=1 } { print }' "$query_file" \
    | docker exec -i "$container" psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 -At ${psql_args[@]+"${psql_args[@]}"} \
    | awk '/Execution Time/ { print $3 }'
}

make_cold() {
  docker restart "$container" >/dev/null
  docker run --rm --privileged --pid=host alpine sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' >/dev/null
  until docker exec "$container" pg_isready -U "$pg_user" -d "$pg_db" >/dev/null 2>&1; do sleep 1; done
}

median() { sort -n | awk '{ a[NR] = $1 } END { print (NR % 2) ? a[(NR + 1) / 2] : (a[NR / 2] + a[NR / 2 + 1]) / 2 }'; }

header="query,cache"
for i in $(seq 1 "$runs"); do header+=",run$i"; done
echo "$header,median_ms" > "$out_file"

for q in "${queries[@]}"; do
  name=$(basename "${q%.sql}")
  for cache in cold warm; do
    times=()
    [[ "$cache" == "warm" ]] && run_once "$q" >/dev/null
    for i in $(seq 1 "$runs"); do
      [[ "$cache" == "cold" ]] && make_cold
      times+=("$(run_once "$q")")
    done
    med=$(printf '%s\n' "${times[@]}" | median)
    log "$name $cache: ${times[*]} -> median ${med} ms"
    echo "$name,$cache,$(IFS=,; echo "${times[*]}"),$med" >> "$out_file"
  done
done

log "wrote $out_file"
