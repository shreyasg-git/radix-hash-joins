#!/usr/bin/env bash
# Tune the radix hash join on THIS machine. Rules: SPEC.md (frozen, version 1).
#   ./tune.sh            official tuning run (about 3 minutes on the Apple M5)
#   ./tune.sh --quick    checks that everything works, in about 1 second; not a result
# Needs a C compiler and make. The PDF needs python3 with matplotlib; without
# it you still get every CSV file and the answer in the terminal.
# Set TUNE_NO_OPEN=1 to keep the PDF from opening.
set -euo pipefail
cd "$(dirname "$0")"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--quick" ) ]]; then
  echo "usage: ./tune.sh [--quick]"; exit 2
fi
make -s
./bin/tune --test
suffix=""; [[ "${1:-}" == "--quick" ]] && suffix="_quick"
out="runs/$(./bin/tune --machine-tag)_$(date +%Y-%m-%d_%H%M%S)$suffix"
./bin/tune "$out" "$@"
if python3 -c "import matplotlib" 2>/dev/null; then
  python3 report.py "$out"
  if [[ -z "${TUNE_NO_OPEN:-}" ]]; then
    if command -v open >/dev/null; then open "$out/report.pdf"
    elif command -v xdg-open >/dev/null; then xdg-open "$out/report.pdf" >/dev/null 2>&1 || true; fi
  fi
else
  echo "python3 with matplotlib not found: no PDF. The CSV files are in $out/"
fi
