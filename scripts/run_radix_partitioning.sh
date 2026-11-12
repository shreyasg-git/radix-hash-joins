#!/usr/bin/env bash
# Radix partitioning, end to end: build, correctness tests, benchmark, report.
# Works on macOS and Linux.
#
#   scripts/run_radix_partitioning.sh            # full run -> results/radix-partitioning, reports/radix-partitioning.pdf
#   QUICK=1 scripts/run_radix_partitioning.sh    # 1-2 minute check -> results/radix-partitioning-quick
#
# Plug the laptop in and close other programs while it runs.
set -euo pipefail
cd "$(dirname "$0")/.."

# Keep a Mac awake for the whole run.
if [[ "$(uname -s)" == Darwin && -z "${CAFFEINATED:-}" ]] && command -v caffeinate >/dev/null; then
  CAFFEINATED=1 exec caffeinate -i "$0" "$@"
fi

NAME=radix-partitioning
[[ "${QUICK:-0}" == 1 ]] && NAME=radix-partitioning-quick
OUT=results/$NAME
if [[ -e "$OUT" ]]; then
  echo "$OUT already exists. Move or rename it first; earlier results are never overwritten."
  exit 1
fi

# Python packages for the report: use a local virtual environment if needed.
if python3 -c "import numpy, matplotlib, reportlab" 2>/dev/null; then
  PY=python3
else
  if [[ ! -x .venv/bin/python3 ]]; then
    echo "Setting up Python packages in .venv (one time)..."
    python3 -m venv .venv
    .venv/bin/python3 -m pip install --quiet --upgrade pip
    .venv/bin/python3 -m pip install --quiet -r requirements.txt
  fi
  PY=.venv/bin/python3
fi

echo "== Build and correctness tests"
make all
make test

echo "== Benchmark (this is the long part)"
if [[ "${QUICK:-0}" == 1 ]]; then
  ./bin/radix_partition "$OUT" --rows 2097152 --repeats 3 --max-bits 16
else
  ./bin/radix_partition "$OUT"
fi

{ uname -a
  if command -v lscpu >/dev/null; then lscpu
  else
    echo "machdep.cpu.brand_string: $(sysctl -n machdep.cpu.brand_string)"
    sysctl hw.ncpu hw.perflevel0.physicalcpu hw.perflevel1.physicalcpu hw.memsize hw.pagesize \
           hw.perflevel0.l1dcachesize hw.perflevel0.l2cachesize hw.l1dcachesize hw.l2cachesize 2>/dev/null || true
  fi
} > "$OUT/system.txt"

echo "== Report"
$PY analysis/analyze_partition.py "$OUT" --pdf "reports/$NAME.pdf"
echo
echo "Done: $OUT and reports/$NAME.pdf"
