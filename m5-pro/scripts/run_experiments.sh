#!/usr/bin/env bash
# Runs every experiment on this Mac, one after another, and saves raw results.
# Reports are made afterwards by scripts/make_reports.sh.
#
#   scripts/run_experiments.sh            full run, results/ (about 18 minutes on the M5)
#   QUICK=1 scripts/run_experiments.sh    small sizes, results-quick/ (a few minutes)
#
# An experiment whose folder already has a .done marker is skipped, so the
# script can be rerun after an interruption. A folder without the marker is
# moved aside as NAME.incomplete-TIME and that experiment starts again.
# Plug the laptop in and close other programs while it runs.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "$(uname -s)" == Darwin && -z "${CAFFEINATED:-}" ]] && command -v caffeinate >/dev/null; then
  CAFFEINATED=1 exec caffeinate -i "$0" "$@"
fi

QUICK=${QUICK:-0}
R=results; [[ "$QUICK" == 1 ]] && R=results-quick
mkdir -p "$R"

system_info() {
  { uname -a
    echo "machdep.cpu.brand_string: $(sysctl -n machdep.cpu.brand_string)"
    sysctl hw.ncpu hw.perflevel0.physicalcpu hw.perflevel1.physicalcpu hw.memsize hw.pagesize hw.cachelinesize \
           hw.perflevel0.l1dcachesize hw.perflevel0.l2cachesize hw.perflevel0.cpusperl2 \
           hw.perflevel1.l1dcachesize hw.perflevel1.l2cachesize hw.l1dcachesize hw.l2cachesize 2>/dev/null || true
    sw_vers 2>/dev/null || true
    pmset -g batt 2>/dev/null | head -2 || true
  } > "$1/system.txt"
}

# run NAME COMMAND... : COMMAND writes into $R/NAME (it creates the folder itself)
run() {
  local name=$1; shift
  local dir="$R/$name"
  if [[ -e "$dir/.done" ]]; then echo "== $name: already done, skipped"; return; fi
  if [[ -e "$dir" ]]; then mv "$dir" "$dir.incomplete-$(date +%H%M%S)"; fi
  echo "== $name  (started $(date +%H:%M:%S))"
  local t0=$SECONDS
  "$@"
  system_info "$dir"
  echo "elapsed_seconds=$((SECONDS - t0))" > "$dir/.done"
  echo "== $name finished in $((SECONDS - t0)) s"
}

make all
make test

if [[ "$QUICK" == 1 ]]; then
  run 01-stride-scan ./bin/scan_benchmark "$R/01-stride-scan" 5
  run 02-simple-hash-join ./bin/hash_join "$R/02-simple-hash-join" 3 500000
  run 02-simple-hash-join-quarter-buckets ./bin/hash_join_quarter "$R/02-simple-hash-join-quarter-buckets" 3 500000
  run 03-radix-partition ./bin/radix_partition "$R/03-radix-partition" --rows 2097152 --repeats 3 --max-bits 16
  run 04-radix-join ./bin/radix_join "$R/04-radix-join" --sizes 100000,1000000,4000000 --bits 0,4,8,12 --repeats 3
  for seed in 7 2026; do
    run "04-radix-join-seed$seed" ./bin/radix_join "$R/04-radix-join-seed$seed" --sizes 1000000,4000000 \
      --bits 0,8,12 --passes 1 --repeats 3 --seed "$seed"
  done
  PAR_ROWS=4000000
else
  run 01-stride-scan ./bin/scan_benchmark "$R/01-stride-scan" 30
  run 02-simple-hash-join ./bin/hash_join "$R/02-simple-hash-join"
  run 02-simple-hash-join-quarter-buckets ./bin/hash_join_quarter "$R/02-simple-hash-join-quarter-buckets"
  run 03-radix-partition ./bin/radix_partition "$R/03-radix-partition"
  run 04-radix-join ./bin/radix_join "$R/04-radix-join"
  # Robustness check: other shuffles of the same data, smaller grid.
  for seed in 7 2026; do
    run "04-radix-join-seed$seed" ./bin/radix_join "$R/04-radix-join-seed$seed" --sizes 1000000,8000000,32000000 \
      --bits 0,6,8,10,12,14,16 --passes 1 --repeats 7 --seed "$seed"
  done
  PAR_ROWS=16000000
fi

# Threads: use the radix setting that was fastest in experiment 4 at this input size.
read -r PAR_BITS PAR_PASSES < <(python3 - "$R/04-radix-join/timings.csv" "$PAR_ROWS" <<'EOF'
import csv, sys, statistics
from collections import defaultdict
runs = defaultdict(list)
for r in csv.DictReader(open(sys.argv[1])):
    if r["algorithm"] == "radix" and int(r["rows_per_input"]) == int(sys.argv[2]):
        runs[int(r["total_bits"]), int(r["passes"])].append(float(r["total_ms"]))
if runs:
    bits, passes = min(runs, key=lambda k: statistics.median(runs[k]))
else:
    bits, passes = 14, 1
print(bits, passes)
EOF
)
echo "== Threads experiment uses $PAR_BITS radix bits in $PAR_PASSES pass(es), fastest at $PAR_ROWS rows in experiment 4"
if [[ "$QUICK" == 1 ]]; then
  run 05-parallel-join ./bin/parallel_join "$R/05-parallel-join" --rows "$PAR_ROWS" --threads 1,2,4 \
    --bits "$PAR_BITS" --passes "$PAR_PASSES" --repeats 3
else
  run 05-parallel-join ./bin/parallel_join "$R/05-parallel-join" --rows "$PAR_ROWS" --threads 1,2,4,8 \
    --bits "$PAR_BITS" --passes "$PAR_PASSES"
fi
# Experiment 6: rows per bucket (both joins, 4 sizes), then hash functions x key patterns.
if [[ "$QUICK" == 1 ]]; then
  run 06-bucket-sweep ./bin/bucket_sweep "$R/06-bucket-sweep" --sizes 65536,1048576 --loads 0.25,0.5,1,4 --repeats 3
  run 06-hash-functions ./bin/bucket_sweep "$R/06-hash-functions" --sizes 262144 --loads 0.5 \
    --hashes identity,multiply-low,fibonacci,crc32,mix --keys dense,stride64,random --repeats 3
else
  run 06-bucket-sweep ./bin/bucket_sweep "$R/06-bucket-sweep"
  run 06-hash-functions ./bin/bucket_sweep "$R/06-hash-functions" --sizes 1048576 --loads 0.5,4 \
    --hashes identity,multiply-low,fibonacci,crc32,mix --keys dense,stride64,random
fi
# Before/after checks for the two fixed design flaws (DESIGN-FLAWS.md): the same
# program with the old v2 behaviour switched back on, run right after the fixed one.
if [[ "$QUICK" == 1 ]]; then
  run 04-radix-join-old-timing ./bin/radix_join "$R/04-radix-join-old-timing" --sizes 100000 --bits 0,8,12,14 \
    --passes 1 --repeats 3 --timing per-partition
  run 05-parallel-join-old-insert ./bin/parallel_join "$R/05-parallel-join-old-insert" --rows "$PAR_ROWS" \
    --threads 1,2,4 --bits "$PAR_BITS" --passes "$PAR_PASSES" --repeats 3 --insert cas-release
else
  run 04-radix-join-old-timing ./bin/radix_join "$R/04-radix-join-old-timing" --sizes 100000,1000000 \
    --timing per-partition
  run 05-parallel-join-old-insert ./bin/parallel_join "$R/05-parallel-join-old-insert" --rows "$PAR_ROWS" \
    --threads 1,2,4,8 --bits "$PAR_BITS" --passes "$PAR_PASSES" --insert cas-release
fi
echo "== All experiments done: $R"
