#!/usr/bin/env python3
"""Writes the settings for the race: the paper's formula against the tuner.

Usage:
    python3 plan.py TUNER_RUN_DIR OUT_DIR [--quick]

Writes OUT_DIR/plan.csv (one setting per line, read by bin/race) and
OUT_DIR/plan.txt (how every setting was worked out, in words).

Settings at every size:
  tuner         the tuner's pick: its winner at a size it learned from,
                otherwise its learned rule
  paper         the paper's formula: bits from formula 1 (page 63, first
                cache, 12 bytes per row), passes from formula 2 (page 60,
                64-entry page-address list); rows per box the same as the
                tuner's, because the paper has no rule for it
  paper-1-pass  the paper's bits in 1 pass, same rows per box: what formula 2
                gives if this machine's page-address list holds at least
                2^bits entries (macOS does not report the number)
  first-race    the version of the first race on 23 November: 20 bytes per
                row, passes from formula 2, 1/2 row per box
  no-cutting    one hash table on all of R, same rows per box as the tuner
"""
import csv
import math
import subprocess
import sys
from pathlib import Path

PAPER_ROW_BYTES = 12      # page 63: B = log2(C * 12 / cache size)
FIRST_RACE_ROW_BYTES = 20  # our table at 1/2 row per box: 8 (row) + 4 (chain link) + 8 (2 boxes of 4 bytes)
PAPER_TLB_ENTRIES = 64     # pages 59 and 60: the paper's computer; macOS does not report this machine's
SIZES = [2**20, 2**22, 2**23, 2**24]
QUICK_SIZES = [2**16, 2**18]
MAX_BITS, MIN_PARTITION_LOG2 = 18, 4  # the tuner's limits (src/tune.c)
LABELS = ["tuner", "paper", "paper-1-pass", "first-race", "no-cutting"]


def kv(path):
    return dict(line.rstrip("\n").split("=", 1) for line in open(path) if "=" in line)


def first_cache_bytes():
    """The first (L1) data cache of the fast cores, as the operating system reports it."""
    for cmd in (["sysctl", "-n", "hw.perflevel0.l1dcachesize"], ["sysctl", "-n", "hw.l1dcachesize"],
                ["getconf", "LEVEL1_DCACHE_SIZE"]):
        try:
            v = int(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip())
        except (OSError, subprocess.CalledProcessError, ValueError):
            continue
        if v > 0:
            return v, " ".join(cmd)
    sys.exit("plan.py: cannot read the first-cache size")


def fit_bits(n, row_bytes, cache):
    """Formula 1, rounded up so one piece really fits: the smallest B with (n / 2^B) * row_bytes <= cache."""
    x = n * row_bytes / cache
    return 0 if x <= 1 else math.ceil(math.log2(x) - 1e-12)


def paper_passes(bits):
    """Formula 2: at most log2(entries) bits per pass."""
    return math.ceil(bits / math.log2(PAPER_TLB_ENTRIES)) if bits else 0


def tuner_pick(run, n):
    best = {int(r["rows_per_input"]): r for r in csv.DictReader(open(run / "best.csv")) if r["role"] == "train"}
    if n in best:
        r = best[n]
        return int(r["bits"]), int(r["passes"]), float(r["rows_per_bucket"]), "the tuner's winner at this size"
    rule = kv(run / "rule.txt")
    target = float(rule["rows_per_partition"])
    raw = math.log2(n / target)
    bits = 0 if raw < 0.5 else math.floor(raw + 0.5)  # same rounding as the tuner (lround)
    bits = min(bits, min(MAX_BITS, max(0, int(math.log2(n)) - MIN_PARTITION_LOG2)))
    passes = min(int(rule["passes"]), bits) if bits else 0
    return bits, passes, float(rule["rows_per_bucket"]), (
        f"the tuner's rule: round(log2({n:,} / {target:,.0f})) = round({raw:.3f}) = {bits} bits")


def load_text(x):
    return f"1/{round(1 / x)}" if x < 1 else f"{x:g}"


def main():
    if len(sys.argv) not in (3, 4) or (len(sys.argv) == 4 and sys.argv[3] != "--quick"):
        sys.exit("usage: python3 plan.py TUNER_RUN_DIR OUT_DIR [--quick]")
    run, out = Path(sys.argv[1]), Path(sys.argv[2])
    quick = len(sys.argv) == 4
    if kv(run / "machine.txt").get("spec") != "1":
        sys.exit("plan.py: the tuner run must be spec version 1")
    cache, source = first_cache_bytes()
    lines = [
        f"tuner_run={run.resolve().name}",
        f"first_cache_bytes={cache} (from: {source})",
        f"paper_row_bytes={PAPER_ROW_BYTES}",
        f"paper_tlb_entries={PAPER_TLB_ENTRIES} (the paper's computer; this machine's is not reported)",
        f"first_race_row_bytes={FIRST_RACE_ROW_BYTES}",
        "",
    ]
    plan = []
    for n in (QUICK_SIZES if quick else SIZES):
        tb, tp, tl, why = tuner_pick(run, n)
        pb = fit_bits(n, PAPER_ROW_BYTES, cache)
        pp = paper_passes(pb)
        fb = fit_bits(n, FIRST_RACE_ROW_BYTES, cache)
        fp = paper_passes(fb)
        x = n * PAPER_ROW_BYTES / cache
        xf = n * FIRST_RACE_ROW_BYTES / cache
        lines += [
            f"== {n:,} rows",
            f"tuner: {tb} bits, {tp} pass(es), {load_text(tl)} row per box ({why})",
            f"paper bits: {n:,} x {PAPER_ROW_BYTES} / {cache:,} = {x:,.3f}; log2 = {math.log2(x):.3f}; rounded up = {pb}",
            f"paper passes: {pb} / log2({PAPER_TLB_ENTRIES}) = {pb} / 6 = {pb / 6:.2f}; rounded up = {pp}",
            f"first-race bits: {n:,} x {FIRST_RACE_ROW_BYTES} / {cache:,} = {xf:,.3f}; log2 = {math.log2(xf):.3f}; "
            f"rounded up = {fb}; passes = {fp}",
            "",
        ]
        plan += [
            (n, "tuner", tb, tp, tl),
            (n, "paper", pb, pp, tl),
            (n, "paper-1-pass", pb, 1 if pb else 0, tl),
            (n, "first-race", fb, fp, 0.5),
            (n, "no-cutting", 0, 0, tl),
        ]
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "plan.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rows_per_input", "label", "bits", "passes", "rows_per_bucket"])
        w.writerows([n, label, b, p, f"{load:g}"] for n, label, b, p, load in plan)
    (out / "plan.txt").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
