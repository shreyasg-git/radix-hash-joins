#!/usr/bin/env bash
# Experiment 1, step 4: MonetDB's join against the tuner's pick, on THIS machine.
#   ./run.sh            4 sizes (1,048,576 to 16,777,216 rows), 10 rounds
#   ./run.sh --quick    checks that everything works, small sizes, 3 rounds; not a result
# The tuner's picks come from the race's plan (RACE_PLAN=..., default: the
# committed Apple M5 race of 2026-11-24). Needs MonetDB in ../install (../setup.sh).
set -euo pipefail
cd "$(dirname "$0")"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--quick" ) ]]; then
  echo "usage: ./run.sh [--quick]"; exit 2
fi
race_plan="${RACE_PLAN:-../../tuner/paper-race/runs/apple-m5_2026-11-24_120145/plan.csv}"
make -s step4
quick=""; rounds=10
if [[ "${1:-}" == "--quick" ]]; then quick="_quick"; rounds=3; fi
out="runs/$(./bin/step4 --machine-tag)_$(date +%Y-%m-%d_%H%M%S)_step4${quick}"
mkdir -p "$out"

# plan.csv: per size, the tuner's pick (copied from the race's plan) and MonetDB.
{
  echo "rows_per_input,label,bits,passes,rows_per_bucket"
  if [[ -n "$quick" ]]; then
    for n in 65536 262144; do echo "$n,tuner,8,1,0.125"; echo "$n,monetdb,0,0,0"; done
  else
    awk -F, 'NR > 1 && $2 == "tuner" { print; print $1 ",monetdb,0,0,0" }' "$race_plan"
  fi
} > "$out/plan.csv"
echo "tuner picks from: $race_plan" > "$out/source.txt"
echo "monetdb_commit=$(git -C ../source rev-parse HEAD 2>/dev/null || echo unknown)" >> "$out/source.txt"

# Keep the Mac awake while timing, as the experiment scripts do.
if command -v caffeinate >/dev/null; then caffeinate -i ./bin/step4 "$out/plan.csv" "$out" "$rounds"
else ./bin/step4 "$out/plan.csv" "$out" "$rounds"; fi
