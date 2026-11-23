#!/usr/bin/env bash
# Live lab: pick an experiment, type its settings, get its PDF report.
#
#   scripts/live.sh            asks which experiment, then every setting
#   scripts/live.sh 6          starts at experiment 6
#   scripts/live.sh 6 again    reruns experiment 6 with the settings typed last time
#
# Press Enter to keep the value in [brackets]. The first time it is the
# default; after that it is what you typed last time for that experiment.
#
# The C program runs exactly as in the official runs and writes its raw CSV
# files; then the same report script as for reports/NN-name.pdf builds the PDF.
# Every run gets its own folder, and the official results/ and reports/ are
# never touched:
#   live/NN-name/DATE-TIME/results/     what the C program wrote (CSV + environment)
#   live/NN-name/DATE-TIME/report.pdf   the report
#   live/NN-name/DATE-TIME/settings.txt what was typed and the exact command
#   live/latest.pdf                     a copy of the newest report
# The terminal then compares your numbers with the official run's.
# Set LIVE_NO_OPEN=1 to keep the PDF from opening in Preview.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p live

PY=python3
if ! $PY -c "import numpy, matplotlib, scipy, reportlab, pypdf" 2>/dev/null; then
  [[ -x .venv/bin/python3 ]] || { python3 -m venv .venv; .venv/bin/python3 -m pip install --quiet numpy matplotlib scipy reportlab pypdf; }
  PY=.venv/bin/python3
fi
make -s all >/dev/null

bold=$'\033[1m'; dim=$'\033[2m'; off=$'\033[0m'

# ask VAR "question" default [check]
# check: int | ints | loads | names | any
ask() {
  local var=$1 question=$2 default=$3 check=${4:-any} answer
  local last; eval "last=\${LAST_$var:-}"
  [[ -n "$last" ]] && default=$last
  while true; do
    read -r -p "  $question ${dim}[$default]${off}: " answer || exit 1
    answer=${answer:-$default}
    answer=${answer// /}
    local re='.*'
    case $check in
      int)   re='^[0-9]+$' ;;
      ints)  re='^[0-9]+(,[0-9]+)*$' ;;
      loads) re='^[0-9.]+(/[0-9]+)?(,[0-9.]+(/[0-9]+)?)*$' ;;
      names) re='^[a-z0-9-]+(,[a-z0-9-]+)*$' ;;
    esac
    [[ "$answer" =~ $re ]] && break
    echo "    That does not look right; expected: $check (comma-separated where a list is allowed)."
  done
  printf -v "$var" '%s' "$answer"
  ANSWERS+="$var=$answer"$'\n'
}

# 1/4 -> 0.25, keeps plain numbers
fractions() { echo "$1" | tr ',' '\n' | awk -F/ '{ printf "%s%g", (NR > 1 ? "," : ""), (NF == 2 ? $1 / $2 : $1) }'; }

# Adds ITEM to a comma list if it is missing.
ensure_in() { local list=$1 item=$2; [[ ",$list," == *",$item,"* ]] && echo "$list" || echo "$item,$list"; }

system_info() {
  { uname -a
    echo "machdep.cpu.brand_string: $(sysctl -n machdep.cpu.brand_string 2>/dev/null)"
    sysctl hw.ncpu hw.perflevel0.physicalcpu hw.perflevel1.physicalcpu hw.memsize hw.pagesize hw.cachelinesize \
           hw.perflevel0.l1dcachesize hw.perflevel0.l2cachesize hw.perflevel0.cpusperl2 \
           hw.perflevel1.l1dcachesize hw.perflevel1.l2cachesize hw.l1dcachesize hw.l2cachesize 2>/dev/null || true
    sw_vers 2>/dev/null || true
    pmset -g batt 2>/dev/null | head -2 || true
  } > "$1/system.txt"
}

# Rough run time in seconds from the settings (per-row costs measured on the M5).
estimate() { $PY - "$@" <<'EOF'
import sys
exp, args = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:])
L = lambda k: [float(x) for x in args[k].split(",")]
rounds = int(args.get("repeats", "5")) + 1
ns = 0.0
if exp == "4":
    for n in L("sizes"):
        sim = 12 if n <= 2e5 else 30
        for b in L("bits"):
            ns += n * (sim if b == 0 else 13) * (1 if b == 0 else len(L("passes")))
elif exp == "5":
    for t in L("threads"):
        ns += L("rows")[0] * (60 + 15) / min(t, 4)
elif exp == "6":
    joins = args["joins"].split(",")
    for n in L("sizes"):
        for load in L("loads"):
            for h in args["hashes"].split(","):
                for k in args["keys"].split(","):
                    # low-bit hashes put multiples of 64 into 1 bucket in 64: chains 64 times longer
                    bad = 64 if h in ("identity", "multiply-low") and k == "stride64" else 1
                    ns += n * (((10 if n <= 1 << 20 else 25) * (1 + load * bad) if "simple" in joins else 0)
                               + (13 if "radix" in joins else 0))
elif exp == "3":
    ns = L("rows")[0] * 3 * (int(args["max_bits"]) + 1) * len(args["passes"].split(","))
elif exp == "2":
    ns = sum(n * 30 for n in (10000, 100000, 500000, 1000000, 2000000, 5000000) if n <= L("max_rows")[0])
elif exp == "1":
    ns = 6e7
print(max(1, round(ns * rounds / 1e9)))
EOF
}

NAMES=("" "01-stride-scan" "02-simple-hash-join" "03-radix-partition" "04-radix-join" "05-parallel-join" "06-bucket-sweep")

choice=${1:-}
if [[ -z "$choice" ]]; then
  echo "${bold}Live lab: M5 Pro experiments${off}  (official results and reports are never changed)"
  echo "  1) Stride scan            memory reads at growing distances          ~4 s"
  echo "  2) Simple hash join       one hash table, growing inputs             ~2 s"
  echo "  3) Radix partitioning     split cost per number of bits and passes   ~3 s"
  echo "  4) Simple vs radix join   which join is faster, and with what bits   ~4 s"
  echo "  5) Threads                speedup with 1, 2, 4 threads               ~3 s"
  echo "  6) Bucket count and hash  rows per bucket, hash function, key pattern ~7 s"
  read -r -p "Which experiment? " choice || exit 1
fi
[[ "$choice" =~ ^[1-6]$ ]] || { echo "Choose 1 to 6."; exit 1; }
NAME=${NAMES[$choice]}
LASTFILE="live/.last-$NAME.sh"
if [[ -f "$LASTFILE" ]]; then
  # shellcheck disable=SC1090
  source "$LASTFILE"
fi
if [[ "${2:-}" == again && ! -f "$LASTFILE" ]]; then echo "No earlier settings for $NAME."; exit 1; fi
if [[ "${2:-}" == again ]]; then exec < <(yes ""); fi

OUT="live/$NAME/$(date +%Y-%m-%d_%H%M%S)"
mkdir -p "$OUT"
RES="$OUT/results"
ANSWERS=""
OFFICIAL=""
echo "${bold}$NAME${off}: Enter keeps the value in [brackets]."
case $choice in
  1)
    ask repeats "Repeats per stride (at least 5)" 5 int
    CMD=(./bin/scan_benchmark "$RES" "$repeats")
    REPORT=(analysis/report_stride_scan.py "$RES" --pdf "$OUT/report.pdf")
    OFFICIAL=results/01-stride-scan
    EST=$(estimate 1 repeats="$repeats") ;;
  2)
    ask rule "Bucket rule: week2 (2 buckets per row) or quarter (1 bucket per 4 rows)" week2 names
    ask repeats "Repeats" 5 int
    ask max_rows "Largest input in rows (10000 to 5000000)" 2000000 int
    bin=./bin/hash_join; OFFICIAL=results/02-simple-hash-join
    if [[ "$rule" == quarter ]]; then bin=./bin/hash_join_quarter; OFFICIAL=results/02-simple-hash-join-quarter-buckets; fi
    CMD=("$bin" "$RES" "$repeats" "$max_rows")
    REPORT=(analysis/report_simple_hash_join.py "$RES" --pdf "$OUT/report.pdf")
    EST=$(estimate 2 repeats="$repeats" max_rows="$max_rows") ;;
  3)
    ask rows "Rows" 4194304 int
    ask max_bits "Largest number of radix bits (1 to 22)" 16 int
    ask passes "Passes" 1,2 ints
    ask repeats "Repeats" 3 int
    CMD=(./bin/radix_partition "$RES" --rows "$rows" --max-bits "$max_bits" --passes "$passes" --repeats "$repeats")
    REPORT=(analysis/analyze_partition.py "$RES" --pdf "$OUT/report.pdf")
    OFFICIAL=results/03-radix-partition
    EST=$(estimate 3 rows="$rows" max_bits="$max_bits" passes="$passes" repeats="$repeats") ;;
  4)
    ask sizes "Rows per input" 1000000,4000000 ints
    ask bits "Radix bits (0 = simple join)" 0,6,8,10,12,14 ints
    ask passes "Passes" 1 ints
    ask repeats "Repeats" 5 int
    ask timing "Timing: clean, or per-partition (the old v2 way, with clock reads inside)" clean names
    bits=$(ensure_in "$bits" 0)
    CMD=(./bin/radix_join "$RES" --sizes "$sizes" --bits "$bits" --passes "$passes" --repeats "$repeats" --timing "$timing")
    REPORT=(analysis/analyze_radix_join.py "$RES" --partition-results results/03-radix-partition --pdf "$OUT/report.pdf")
    OFFICIAL=results/04-radix-join
    EST=$(estimate 4 sizes="$sizes" bits="$bits" passes="$passes" repeats="$repeats") ;;
  5)
    ask rows "Rows per input" 4000000 int
    ask threads "Threads" 1,2,4 ints
    ask bits "Radix bits" 10 int
    ask passes "Passes" 1 int
    ask repeats "Repeats" 5 int
    ask insert "Simple join insert: swap, or cas-release (the old v2 way)" swap names
    threads=$(ensure_in "$threads" 1)
    CMD=(./bin/parallel_join "$RES" --rows "$rows" --threads "$threads" --bits "$bits" --passes "$passes" --repeats "$repeats" --insert "$insert")
    REPORT=(analysis/analyze_parallel.py "$RES" --pdf "$OUT/report.pdf")
    OFFICIAL=results/05-parallel-join
    EST=$(estimate 5 rows="$rows" threads="$threads" repeats="$repeats") ;;
  6)
    ask sizes "Rows per input (65536, 524288, 4194304, 16777216 match the official run)" 4194304 ints
    ask loads "Rows per bucket (1/4 works too)" 1/8,1/4,1/2,1,2,4 loads
    ask hashes "Hash functions: identity, multiply-low, fibonacci, crc32, mix" mix names
    ask keys "Key patterns: dense, stride64, random" dense names
    ask joins "Joins: simple, radix" simple,radix names
    ask bits "Radix bits, or auto (about 4096 rows per partition)" auto any
    ask repeats "Repeats" 3 int
    CMD=(./bin/bucket_sweep "$RES" --sizes "$sizes" --loads "$(fractions "$loads")" --hashes "$hashes" --keys "$keys"
         --joins "$joins" --bits "$bits" --repeats "$repeats")
    REPORT=(analysis/analyze_bucket_sweep.py "$RES" --pdf "$OUT/report.pdf")
    OFFICIAL="results/06-bucket-sweep results/06-hash-functions"
    EST=$(estimate 6 sizes="$sizes" loads="$(fractions "$loads")" hashes="$hashes" keys="$keys" joins="$joins" repeats="$repeats") ;;
esac

# Remember the answers for next time (also when the run fails, so a typo is easy to fix).
echo "$ANSWERS" | sed '/^$/d; s/^/LAST_/; s/=\(.*\)$/="\1"/' > "$LASTFILE"
{ echo "experiment=$NAME"; echo "$ANSWERS"; echo "command=${CMD[*]}"; echo "report=$PY ${REPORT[*]}"; } > "$OUT/settings.txt"

echo
echo "Running: ${dim}${CMD[*]}${off}"
echo "Rough estimate: about $EST s. Ctrl-C stops it."
trap 'echo; echo "Stopped. Partial results stay in $OUT"; exit 130' INT
t0=$(date +%s)
if ! "${CMD[@]}"; then
  echo "The experiment program stopped with an error (message above). Nothing was reported."
  echo "Run scripts/live.sh $choice again: your last answers are the new defaults, so fix the one that failed."
  exit 1
fi
system_info "$RES"
t1=$(date +%s)
echo "Building the report..."
REPORT_ECHO=1 "$PY" "${REPORT[@]}" > "$OUT/report-text.txt"
t2=$(date +%s)
cp "$OUT/report.pdf" live/latest.pdf
trap - INT

echo
echo "${bold}Done.${off} Experiment $((t1 - t0)) s, report $((t2 - t1)) s."
echo "  report:  $OUT/report.pdf   (also live/latest.pdf)"
echo "  data:    $RES/  (timings.csv and friends; any tool can read them)"
echo
if [[ "$(head -c 1 "$OUT/report-text.txt")" != "{" ]]; then grep -vE '^(Wrote|Report:)' "$OUT/report-text.txt" || true; fi
# shellcheck disable=SC2086
"$PY" analysis/live_summary.py "$choice" "$RES" $OFFICIAL || true
if [[ -z "${LIVE_NO_OPEN:-}" ]] && command -v open >/dev/null; then open "$OUT/report.pdf"; fi
