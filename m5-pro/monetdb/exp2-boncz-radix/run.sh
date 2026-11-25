#!/usr/bin/env bash
# Experiment 2, step 3: Boncz's join against the tuner's join, on THIS machine.
#   ./run.sh            4 sizes (1,048,576 to 16,777,216 rows), 10 rounds
#   ./run.sh --quick    checks that everything works, small sizes, 3 rounds; not a result
# Settings come from the race's plan (RACE_PLAN=..., default: the committed
# Apple M5 race of 2026-11-24): its "tuner" and "paper" lines.
set -euo pipefail
cd "$(dirname "$0")"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--quick" ) ]]; then
  echo "usage: ./run.sh [--quick]"; exit 2
fi
race_plan="${RACE_PLAN:-../../tuner/paper-race/runs/apple-m5_2026-11-24_120145/plan.csv}"
make -s step3
quick=""; rounds=10
if [[ "${1:-}" == "--quick" ]]; then quick="_quick"; rounds=3; fi
out="runs/$(./bin/step3 --machine-tag)_$(date +%Y-%m-%d_%H%M%S)_step3${quick}"
mkdir -p "$out"

# Per size: tuner = the tuner's join at its pick; boncz-tuner = Boncz's join at
# the same bits and passes; boncz-paper = Boncz's join at the paper's formula.
{
  echo "rows_per_input,label,bits,passes,rows_per_bucket"
  if [[ -n "$quick" ]]; then
    for n in 65536 262144; do
      echo "$n,tuner,8,1,0.125"; echo "$n,boncz-tuner,8,1,0"; echo "$n,boncz-paper,6,1,0"
    done
  else
    awk -F, 'NR > 1 && $2 == "tuner" { print; print $1 ",boncz-tuner," $3 "," $4 ",0" }
             NR > 1 && $2 == "paper" { print $1 ",boncz-paper," $3 "," $4 ",0" }' "$race_plan"
  fi
} > "$out/plan.csv"
echo "settings from: $race_plan" > "$out/source.txt"

if command -v caffeinate >/dev/null; then caffeinate -i ./bin/step3 "$out/plan.csv" "$out" "$rounds"
else ./bin/step3 "$out/plan.csv" "$out" "$rounds"; fi
