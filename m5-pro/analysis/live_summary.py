"""Prints a live run's numbers next to the official run's, setting by setting.

Usage (called by scripts/live.sh):
    python3 analysis/live_summary.py EXPERIMENT LIVE_RESULTS OFFICIAL_RESULTS [MORE_OFFICIAL_RESULTS]
Reads the summary.csv files that the report scripts write. Lower is faster
for every number shown.
"""
import sys
from pathlib import Path

import report_lib as R

# experiment -> (columns that name a setting, column with the result, unit, how to show the setting)
SPEC = {
    "1": (("condition", "stride_bytes"), "median_ns_per_read", "ns per read",
          lambda r: f"{r['condition']}, stride {r['stride_bytes']} B"),
    "2": (("rows_per_input",), "total_median_ms", "ms", lambda r: f"{int(r['rows_per_input']):,} rows"),
    "3": (("page_mode", "passes", "total_bits"), "median_ns_per_row", "ns per row",
          lambda r: f"{r['total_bits']} bits, {r['passes']} pass{'es' if r['passes'] != '1' else ''}"),
    "4": (("rows_per_input", "algorithm", "total_bits", "passes"), "total_median_ms", "ms",
          lambda r: f"{int(r['rows_per_input']):,} rows, " + ("simple" if r["algorithm"] == "simple" else
                    f"radix {r['total_bits']} bits/{r['passes']} pass")),
    "5": (("algorithm", "threads", "phase"), "median_ms", "ms",
          lambda r: f"{r['algorithm']}, {r['threads']} thread{'s' if r['threads'] != '1' else ''}"),
    "6": (("rows_per_input", "join", "hash", "keys", "rows_per_bucket"), "total_median_ms", "ms",
          lambda r: f"{int(r['rows_per_input']):,} rows, {r['join']}, {r['hash']}, {r['keys']}, "
                    f"{float(r['rows_per_bucket']):g} rows/bucket"),
}
LIMIT = 30


def rows_of(folder):
    path = Path(folder) / "summary.csv"
    return R.read_csv(path) if path.exists() else []


def main():
    exp, live, official = sys.argv[1], Path(sys.argv[2]), [Path(p) for p in sys.argv[3:]]
    keys, metric, unit, label = SPEC[exp]
    mine = rows_of(live)
    if exp == "5":
        mine = [r for r in mine if r["phase"] == "total"]
    if not mine:
        print("(no summary.csv to compare)")
        return
    note = ""
    if exp in ("3", "5"):
        # Only comparable when the official run used the same input size and setting.
        env_live = R.read_environment(live)
        env_off = R.read_environment(official[0]) if official and (official[0] / "environment.txt").exists() else {}
        same = ("rows", "radix_bits", "radix_passes") if exp == "5" else ("rows",)
        differ = [k for k in same if env_live.get(k) != env_off.get(k)]
        if differ:
            note = ("The official run used " + ", ".join(f"{k}={env_off.get(k)}" for k in differ)
                    + "; yours used " + ", ".join(f"{k}={env_live.get(k)}" for k in differ)
                    + ", so the official column is left out.")
            official = []
    ref = {}
    for folder in official:
        for r in rows_of(folder):
            if exp == "5" and r["phase"] != "total":
                continue
            ref[tuple(_norm(r[k]) for k in keys)] = float(r[metric])

    mine.sort(key=lambda r: tuple(_sortable(r[k]) for k in keys))
    width = max(len(label(r)) for r in mine[:LIMIT])
    print(f"Your run against the official run ({unit}, median; lower is faster):")
    print(f"  {'setting'.ljust(width)}  {'yours':>10}  {'official':>10}  change")
    for r in mine[:LIMIT]:
        k = tuple(_norm(r[c]) for c in keys)
        yours = float(r[metric])
        if k in ref:
            change = (yours - ref[k]) / ref[k]
            print(f"  {label(r).ljust(width)}  {yours:>10.3f}  {ref[k]:>10.3f}  {change:+.0%}")
        else:
            print(f"  {label(r).ljust(width)}  {yours:>10.3f}  {'not run':>10}")
    big = [abs(float(r[metric]) / ref[k] - 1) for r in mine
           for k in [tuple(_norm(r[c]) for c in keys)] if k in ref]
    if any(x > 0.10 for x in big):
        print("  Note: the same setting can differ by 10-20% between runs, from the run itself and from which other "
              "settings ran in between (see DESIGN-FLAWS.md). Compare settings within your run first.")
    if len(mine) > LIMIT:
        print(f"  ... {len(mine) - LIMIT} more settings in {live / 'summary.csv'}")
    if note:
        print(note)
    if exp in ("4", "6"):
        group = (lambda r: r["rows_per_input"]) if exp == "4" else (lambda r: (r["rows_per_input"], r["join"]))
        best = {}
        for r in mine:
            if exp == "4" and r["algorithm"] == "simple":
                continue
            g = group(r)
            if g not in best or float(r[metric]) < float(best[g][metric]):
                best[g] = r
        print("Fastest of your settings:")
        for g, r in best.items():
            print(f"  {label(r)}: {float(r[metric]):.3f} {unit}")


def _norm(value):
    try:
        return float(value)
    except ValueError:
        return value


def _sortable(value):
    v = _norm(value)
    return (0, v, "") if isinstance(v, float) else (1, 0, v)


if __name__ == "__main__":
    main()
