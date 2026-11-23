#!/usr/bin/env bash
# Builds every M5 Pro report from saved results.
#   scripts/make_reports.sh                          results/ -> reports/
#   RESULTS=results-quick scripts/make_reports.sh    results-quick/ -> reports-quick/
set -euo pipefail
cd "$(dirname "$0")/.."
R=${RESULTS:-results}
OUT=reports; [[ "$R" == results-quick ]] && OUT=reports-quick
mkdir -p "$OUT"
PY=python3
if ! $PY -c "import numpy, matplotlib, scipy, reportlab, pypdf" 2>/dev/null; then
  [[ -x .venv/bin/python3 ]] || { python3 -m venv .venv; .venv/bin/python3 -m pip install --quiet numpy matplotlib scipy reportlab pypdf; }
  PY=.venv/bin/python3
fi
seeds=()
for d in "$R"/04-radix-join-seed*; do [[ -d "$d" && ! "$d" == *incomplete* ]] && seeds+=("$d"); done
set -x
$PY analysis/report_stride_scan.py "$R/01-stride-scan" --pdf "$OUT/01-stride-scan.pdf" >/dev/null
$PY analysis/report_simple_hash_join.py "$R/02-simple-hash-join" --pdf "$OUT/02-simple-hash-join.pdf"
$PY analysis/report_simple_hash_join.py "$R/02-simple-hash-join-quarter-buckets" --pdf "$OUT/02-simple-hash-join-quarter-buckets.pdf"
$PY analysis/analyze_partition.py "$R/03-radix-partition" --pdf "$OUT/03-radix-partition.pdf"
$PY analysis/analyze_radix_join.py "$R/04-radix-join" --partition-results "$R/03-radix-partition" \
  --pdf "$OUT/04-radix-join.pdf" ${seeds[@]+--seed-results "${seeds[@]}"}
$PY analysis/analyze_parallel.py "$R/05-parallel-join" --pdf "$OUT/05-parallel-join.pdf"
$PY analysis/analyze_bucket_sweep.py "$R/06-bucket-sweep" --hash-results "$R/06-hash-functions" \
  --pdf "$OUT/06-bucket-sweep.pdf"
M5_RESULTS="$R" $PY analysis/final_analysis.py --pdf "$OUT/07-final-analysis.pdf"
