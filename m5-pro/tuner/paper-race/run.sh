#!/usr/bin/env bash
# Paper's formula against the tuner, on THIS machine. See README.md.
#   ./run.sh            official run: 4 sizes, 10 rounds (about 30 seconds on the Apple M5)
#   ./run.sh --quick    checks that everything works, in a few seconds; not a result
# TUNER_RUN=../runs/<folder> picks the tuner run whose settings are used
# (default: the newest full run). Set NO_OPEN=1 to keep the PDF from opening.
set -euo pipefail
cd "$(dirname "$0")"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--quick" ) ]]; then
  echo "usage: ./run.sh [--quick]"; exit 2
fi
quick=""; rounds=10
if [[ "${1:-}" == "--quick" ]]; then quick="--quick"; rounds=3; fi
tuner_run="${TUNER_RUN:-$(ls -d ../runs/*/ | grep -v '_quick/$' | sort | tail -1)}"
make -s
out="runs/$(./bin/race --machine-tag)_$(date +%Y-%m-%d_%H%M%S)${quick:+_quick}"
mkdir -p "$out"
python3 plan.py "$tuner_run" "$out" $quick
# Keep the Mac awake while timing, as the experiment scripts do.
if command -v caffeinate >/dev/null; then caffeinate -i ./bin/race "$out/plan.csv" "$out" "$rounds"
else ./bin/race "$out/plan.csv" "$out" "$rounds"; fi
if python3 -c "import matplotlib" 2>/dev/null; then
  python3 report.py "$out"
  if [[ -z "${NO_OPEN:-}" ]] && command -v open >/dev/null; then open "$out/report.pdf"; fi
else
  echo "python3 with matplotlib not found: no PDF. The CSV files are in $out/"
fi
