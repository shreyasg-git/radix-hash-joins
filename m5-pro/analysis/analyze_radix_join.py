"""Experiment 4 analysis: simple hash join against radix hash join, plus a
small cost model in the spirit of the paper.

Usage: python3 analysis/analyze_radix_join.py results/04-radix-join
       [--partition-results results/03-radix-partition] [--pdf reports/04-radix-join.pdf]
Writes summary.csv, model_choices.csv and figures/ into the results folder and the PDF report.
"""
import argparse
import math
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter
from scipy.optimize import nnls

import report_lib as R

# Bytes one row of R occupies in a hash table: record (8), chain link (4) and on
# average about 2.9 bucket entries of 4 bytes (bucket count is the power of two
# at or above twice the rows, so between 2x and 4x the rows).
TABLE_BYTES_PER_ROW = 8 + 4 + 4 * 2.9


def table_bytes(rows):
    buckets = 1
    while buckets < 2 * rows:
        buckets *= 2
    return rows * 12 + buckets * 4


def rows_label(n):
    return f"{n / 1e6:g}M" if n >= 1e6 else f"{n / 1e3:g}K"


def setting(k):
    return f"{k[2]} bits, {k[3]} pass{'es' if k[3] > 1 else ''}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, nargs="?", default=R.ROOT / "results" / "04-radix-join")
    parser.add_argument("--partition-results", type=Path, default=R.ROOT / "results" / "03-radix-partition")
    parser.add_argument("--pdf", type=Path, default=R.REPORTS / "04-radix-join.pdf")
    parser.add_argument("--seed-results", type=Path, nargs="*", default=[],
                        help="extra results folders run with other data seeds (robustness check)")
    args = parser.parse_args()
    env = R.read_environment(args.results)
    rows = R.read_csv(args.results / "timings.csv")
    repeats = int(env["repeats"])

    # ---- check and collect -----------------------------------------------------
    runs = defaultdict(list)
    for r in rows:
        n = int(r["rows_per_input"])
        assert int(r["matches"]) == (n + 1) // 2, "wrong match count"
        runs[n, r["algorithm"], int(r["total_bits"]), int(r["passes"])].append(
            {k: float(r[k]) for k in ("partition_r_ms", "partition_s_ms", "build_ms", "probe_ms", "total_ms",
                                      "max_partition_rows")})
    assert all(len(v) == repeats for v in runs.values()), "missing runs"

    med, mean, tot = {}, {}, {}
    summary = []
    for key, values in sorted(runs.items()):
        for v in values:
            v["partition_ms"] = v["partition_r_ms"] + v["partition_s_ms"]
        fields = ("partition_ms", "build_ms", "probe_ms", "total_ms", "max_partition_rows")
        med[key] = {f: R.summarize([v[f] for v in values])["median"] for f in fields}
        mean[key] = {f: R.summarize([v[f] for v in values])["mean"] for f in fields}
        tot[key] = R.summarize([v["total_ms"] for v in values])
        n, alg, B, P = key
        t = tot[key]
        summary.append({"rows_per_input": n, "algorithm": alg, "total_bits": B, "passes": P, "runs": t["n"],
                        "partition_median_ms": round(med[key]["partition_ms"], 3),
                        "build_median_ms": round(med[key]["build_ms"], 3),
                        "probe_median_ms": round(med[key]["probe_ms"], 3),
                        "total_median_ms": round(t["median"], 3), "total_mean_ms": round(t["mean"], 3),
                        "total_sd_ms": round(t["sd"], 3), "total_cv_percent": round(100 * t["cv"], 2),
                        "total_min_ms": round(t["min"], 3), "total_max_ms": round(t["max"], 3),
                        "ns_per_input_row": round(t["median"] * 1e6 / n, 3),
                        "partition_table_bytes": table_bytes(int(med[key]["max_partition_rows"]))})
    R.write_summary(args.results / "summary.csv", summary)
    typical_cv = sorted(t["cv"] for t in tot.values())[len(tot) // 2]

    sizes = sorted({k[0] for k in med})
    simple_key = {n: (n, "simple", 0, 0) for n in sizes}
    simple = {n: med[simple_key[n]] for n in sizes}
    radix_keys = {n: [k for k in med if k[0] == n and k[1] == "radix"] for n in sizes}
    best = {n: min(radix_keys[n], key=lambda k: tot[k]["median"]) for n in sizes}
    speedup = {n: tot[simple_key[n]]["median"] / tot[best[n]]["median"] for n in sizes}
    raw_total = {k: [v["total_ms"] for v in vs] for k, vs in runs.items()}
    speedup_ci = {n: R.bootstrap_ratio_ci(raw_total[simple_key[n]], raw_total[best[n]]) for n in sizes}
    steal = R.steal_summary(rows, "total_ms")
    # Every recorded radix run faster than every recorded simple run?
    separated = {n: tot[best[n]]["max"] < tot[simple_key[n]]["min"] for n in sizes}
    # Settings whose runs overlap with the best setting's runs: a tie within noise.
    ties = {n: [k for k in radix_keys[n] if k != best[n] and tot[k]["min"] <= tot[best[n]]["max"]] for n in sizes}
    l1, l2, l3 = (R.cache_bytes(env, k) for k in (1, 2, 3))
    biggest = sizes[-1]
    first_win = next((n for n in sizes if separated[n]), None)
    per_row = lambda key: tot[key]["median"] * 1e6 / key[0]

    # ---- cost model -------------------------------------------------------------
    # Build+probe cost per row as a function of the table size s of one partition:
    # for each cache level with capacity C, the share of table accesses that miss
    # it is modeled as max(0, 1 - C/s). Penalties are fitted with non-negative
    # least squares to all settings (like calibrating the paper's model).
    levels = [(name, c) for name, c in (("L1", l1), ("L2", l2), ("L3", l3)) if c]

    def features(s):
        return [1.0] + [max(0.0, 1.0 - c / s) for _, c in levels]

    X, y = [], []
    for (n, alg, B, P), m in med.items():
        X.append(features(table_bytes(int(round(n / (1 << B))))))
        y.append((m["build_ms"] + m["probe_ms"]) * 1e6 / n)
    coef, _ = nnls(np.array(X), np.array(y))

    def join_model(rows_in_partition):
        return float(np.dot(features(table_bytes(max(1, int(round(rows_in_partition))))), coef))

    one_pass = {}
    part_file = args.partition_results / "summary.csv"
    if part_file.exists():
        for r in R.read_csv(part_file):
            if r["page_mode"] in ("4k", "default") and r["passes"] == "1":  # M5 copy: macOS has only "default"
                one_pass[int(r["total_bits"])] = float(r["median_ns_per_row"])
    partition_source = "experiment 3" if one_pass else "this experiment"

    def partition_model(n, B, P):
        bits = [B // P + (i < B % P) for i in range(P)]
        if one_pass and all(b in one_pass for b in bits):
            return 2 * sum(one_pass[b] for b in bits)  # R and S, ns per input row
        m = med.get((n, "radix", B, P))
        return m["partition_ms"] * 1e6 / n if m else float("nan")

    def model_total(n, B, P):
        return partition_model(n, B, P) + join_model(n / (1 << B)) if B else join_model(n)

    model_best = {n: min(radix_keys[n], key=lambda k: model_total(n, k[2], k[3])) for n in sizes}
    regret = {n: tot[model_best[n]]["median"] / tot[best[n]]["median"] - 1 for n in sizes}
    model_tied = {n: model_best[n] == best[n] or model_best[n] in ties[n] for n in sizes}
    R.write_summary(args.results / "model_choices.csv", [
        {"rows_per_input": n, "best_bits": best[n][2], "best_passes": best[n][3],
         "best_ms": round(tot[best[n]]["median"], 3), "model_bits": model_best[n][2],
         "model_passes": model_best[n][3], "model_choice_ms": round(tot[model_best[n]]["median"], 3),
         "model_choice_slower_percent": round(100 * regret[n], 2),
         "model_choice_within_noise": int(model_tied[n])} for n in sizes])
    model_err_simple = [abs(model_total(n, 0, 0) - per_row(simple_key[n])) / per_row(simple_key[n]) for n in sizes]
    model_err_best = [abs(model_total(n, *best[n][2:]) - per_row(best[n])) / per_row(best[n]) for n in sizes]
    rule_l2 = {n: max(1, math.ceil(math.log2(n * TABLE_BYTES_PER_ROW / l2))) if l2 else None for n in sizes}

    # ---- figures ------------------------------------------------------------------
    R.style_charts()
    fmt_rows = FuncFormatter(lambda v, _: rows_label(v))

    fig, ax = R.figure(2.7)
    for i, (label, keys) in enumerate((("simple hash join", [simple_key[n] for n in sizes]),
                                       ("radix hash join, best setting", [best[n] for n in sizes]))):
        R.band(ax, sizes, [tot[k]["q1"] * 1e6 / k[0] for k in keys], [tot[k]["q3"] * 1e6 / k[0] for k in keys],
               R.SERIES[i])
        ax.plot(sizes, [per_row(k) for k in keys], "o-", color=R.SERIES[i], label=label)
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(fmt_rows)
    ax.set_xticks(sizes)
    ax.minorticks_off()
    top = max(per_row(simple_key[n]) for n in sizes) * 1.15
    for c, label in ((l2, "L2"), (l3, "L3")):
        if c and sizes[0] <= c / TABLE_BYTES_PER_ROW <= sizes[-1]:
            x = c / TABLE_BYTES_PER_ROW
            ax.axvline(x, color=R.INK_3, lw=0.8, ls=(0, (2, 2)))
            ax.text(x * 1.06, top * 0.97, f"simple table = {label}", color=R.INK_3, fontsize=7.5, va="top")
    ax.set(xlabel="rows per input (log scale)", ylabel="ns per input row", ylim=(0, top))
    ax.legend(loc="upper left")
    fig1 = R.save(fig, args.results, "01_simple_vs_radix.png")

    fig, ax = R.figure(2.8)
    width = 0.36
    xpos = np.arange(len(sizes))
    phases = [("partition_ms", "partition R and S", R.SERIES[2]), ("build_ms", "build", R.SERIES[0]),
              ("probe_ms", "probe", R.SERIES[1])]
    for j, keys in enumerate(([simple_key[n] for n in sizes], [best[n] for n in sizes])):
        bottom = np.zeros(len(sizes))
        for field, label, color in phases:
            vals = np.array([mean[k][field] * 1e6 / k[0] for k in keys])
            ax.bar(xpos + (j - 0.5) * width * 1.12, vals, width, bottom=bottom, color=color, edgecolor="white",
                   linewidth=0.8, label=label if j == 1 else None)
            bottom += vals
        for x, b in zip(xpos, bottom):
            ax.text(x + (j - 0.5) * width * 1.12, b + 1.2, "simple" if j == 0 else "radix", ha="center",
                    fontsize=6.8, color=R.INK_3)
    ax.set_xticks(xpos, [rows_label(n) for n in sizes])
    ax.set(xlabel="rows per input", ylabel="mean ns per input row")
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper left")
    fig2 = R.save(fig, args.results, "02_phases.png")

    fig, ax = R.figure(2.75)
    for i, P in enumerate(sorted({k[3] for k in radix_keys[biggest]})):
        ks = sorted((k for k in radix_keys[biggest] if k[3] == P), key=lambda k: k[2])
        xs = [k[2] for k in ks]
        R.band(ax, xs, [tot[k]["q1"] for k in ks], [tot[k]["q3"] for k in ks], R.SERIES[i + 1])
        ax.plot(xs, [tot[k]["median"] for k in ks], "o-", color=R.SERIES[i + 1],
                label=f"radix, {P} pass{'es' if P > 1 else ''}")
        ax.plot(xs, [model_total(biggest, k[2], k[3]) * biggest / 1e6 for k in ks], "--", lw=1.2,
                color=R.SERIES[i + 1], label=f"cost model, {P} pass{'es' if P > 1 else ''}")
    ax.axhline(tot[simple_key[biggest]]["median"], color=R.SERIES[0], lw=1.8, label="simple hash join")
    ax.set(xlabel="radix bits B", ylabel="total join time (ms)", ylim=(0, None))
    ax.legend(loc="upper center", ncol=3, bbox_to_anchor=(0.5, 1.16))
    fig3 = R.save(fig, args.results, "03_bits_sweep.png")

    fig, ax = R.figure(2.6)
    for i, P in enumerate(sorted({k[3] for k in radix_keys[biggest]})):
        pts = [(table_bytes(int(round(n / (1 << B)))), (m["build_ms"] + m["probe_ms"]) * 1e6 / n)
               for (n, alg, B, PP), m in med.items() if alg == "radix" and PP == P]
        ax.scatter([x for x, _ in pts], [v for _, v in pts], s=14, color=R.SERIES[i + 1], zorder=3,
                   label=f"radix settings, {P} pass{'es' if P > 1 else ''}")
    ax.scatter([table_bytes(n) for n in sizes], [(simple[n]["build_ms"] + simple[n]["probe_ms"]) * 1e6 / n
                                                 for n in sizes], s=20, color=R.SERIES[0], zorder=3,
               label="simple join")
    xs = np.logspace(math.log10(table_bytes(4)), math.log10(table_bytes(biggest)), 300)
    ax.plot(xs, [float(np.dot(features(x), coef)) for x in xs], color=R.INK, lw=1.2, label="fitted model")
    for name, c in levels:
        ax.axvline(c, color=R.INK_3, lw=0.8, ls=(0, (2, 2)))
        ax.text(c * 1.12, 2, name, color=R.INK_3, fontsize=7.5)
    ax.set_xscale("log")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: R.human_bytes(v)))
    ax.set(xlabel="hash table size of one partition (log scale)", ylabel="build + probe, ns per row", ylim=(0, None))
    ax.legend(loc="upper left")
    fig4 = R.save(fig, args.results, "04_table_size_model.png")

    # ---- report ----------------------------------------------------------------------
    rep = R.Report("Simple hash join against radix hash join",
                   f"Experiment 4  ·  {R.cpu_model(args.results)}  ·  {R.machine_line(env)}")
    bk = best[biggest]
    rep.summary(
        f"At {biggest:,} rows per input the simple hash join took {tot[simple_key[biggest]]['median']:.0f} ms and the "
        f"best radix hash join ({setting(bk)}) {tot[bk]['median']:.0f} ms: {speedup[biggest]:.1f} times as fast, "
        "although it reads and writes both inputs once more to partition them. "
        + (f"From {first_win:,} rows per input upward every radix run was faster than every simple run. "
           if first_win else "")
        + (f"At {sizes[0]:,} rows, where the whole table fits in cache, the two joins were within noise of each "
           f"other." if not separated[sizes[0]] else ""))
    rep.key_numbers([
        (f"{speedup[biggest]:.1f}x", f"radix over simple at {rows_label(biggest)} rows per input"),
        (f"{per_row(simple_key[sizes[0]]):.0f} → {per_row(simple_key[biggest]):.0f} ns",
         "simple join, cost per row as inputs grow"),
        ((lambda lo, hi: f"{lo} ns" if lo == hi else f"{lo}–{hi} ns")(  # M5 copy: no "12–12 ns"
            f"{min(per_row(best[n]) for n in sizes):.0f}", f"{max(per_row(best[n]) for n in sizes):.0f}"),
         "radix join, cost per row at every size"),
        (f"{bk[2]} bits", f"best setting at {rows_label(biggest)} ({bk[3]} pass{'es' if bk[3] > 1 else ''})"),
    ])

    rep.h("What we compared")
    rep.bullets([
        "<b>Simple hash join</b> (the Week 2 baseline): build one hash table on all of R, then probe it with every row "
        "of S. Once the table is larger than the caches, most probes wait for main memory.",
        "<b>Radix hash join</b> (the paper's partitioned hash join): partition R and S on the same B hash bits in P "
        "passes, then join partition p of R with partition p of S using a small table that fits in cache.",
        "Both use the same hash table code, the same Week 2 inputs (unique left keys 0 to N-1, right keys 0, 2, 4, ..., "
        "both shuffled) and the same output: a count of matches and a checksum of the matching row pairs. Every run "
        "is checked against the expected count and checksum.",
    ])

    rep.h("What changed from Week 2")
    rep.bullets([
        "<b>Smaller table.</b> Chain links and bucket entries are 32-bit instead of 64-bit. Both joins use the same "
        "layout.",
        "<b>Different hash bits for buckets.</b> Inside a partition every key shares its low B hash bits, so the radix "
        "join picks buckets from the bits above them. Otherwise all keys of a partition would crowd into a few buckets.",
        "<b>Memory prepared before timing.</b> Week 2 counted allocation and first-touch page faults in the build time. "
        "Here all memory is allocated and written before the timer starts; clearing the bucket array is still build "
        "time. The timing rules differ, so do not divide these times by Week 2's.",
        f"<b>Larger inputs.</b> Up to {biggest:,} rows per input, so the simple join's table "
        f"({R.human_bytes(table_bytes(biggest))}) is far larger than every cache.",
    ])

    rep.h("Method")
    nbits = sorted({k[2] for k in med if k[1] == "radix"})
    # M5 copy: the share of each run spent reading the clock, computed instead of the fixed "under 1%".
    clock_ns = float(env.get("timer_call_ns", 0))
    share = {k: 2 * (1 << k[2]) * clock_ns / (tot[k]["median"] * 1e6) for n in sizes for k in radix_keys[n]}
    best_share = max(share[best[n]] for n in sizes)
    worst = max(share, key=share.get)
    clock_text = (f"They add at most {R.pct(best_share, 2 if best_share < 0.01 else 0)} to the fastest setting at any size"
                  + (f", but {R.pct(share[worst])} to {worst[2]} bits at {worst[0]:,} rows, where partitions hold about "
                     f"{worst[0] / (1 << worst[2]):.0f} rows each." if share[worst] > 0.01 else "."))
    rep.bullets([
        f"<b>Settings.</b> Rows per input: {', '.join(rows_label(n) for n in sizes)}. Radix bits: "
        f"{', '.join(map(str, nbits))} (skipped when partitions would hold fewer than 4 rows), in 1 or 2 passes.",
        f"<b>Repetitions.</b> One verified warmup, then {repeats} recorded runs of every setting. Each round runs "
        f"every setting once in a newly shuffled order, so slow periods affect all settings alike. One thread; "
        f"{R.pinning_text(env)}. Input data seed: {env.get('seed', '42')}.",
        ("<b>Phases.</b> Partition time covers all passes over R and S. Build and probe times are summed over all "
         f"partitions and include two clock reads per partition (about {clock_ns:.0f} ns each). " + clock_text
         if env.get("timing", "").startswith("per-partition") else
         "<b>Phases.</b> Partition time covers all passes over R and S. The build-and-probe loop over all partitions "
         "is timed once, with no clock reads inside it, so the total is not inflated by the timer (v2 read the clock "
         "twice per partition, see DESIGN-FLAWS.md). The build time comes from a separate build-only pass right after "
         "each run, which is not part of the total; the probe time is the join time minus that build time."),
        f"<b>Statistics.</b> Medians in text and tables, interquartile bands in line charts, means in stacked bars "
        f"(so the phases add up). Typical run-to-run variation: {R.pct(typical_cv)} of the mean. Two settings count "
        "as a tie when their run ranges overlap. Speedups carry 95% bootstrap confidence intervals.",
        f"<b>Interference.</b> {R.steal_text(steal)}",
    ])

    rep.h("The main result")
    rep.figure(fig1, "Figure 1. Median time per input row; bands span the middle half of the runs. A dashed line "
                     "marks the input size at which the simple join's hash table reaches a cache size.")
    tbl = [["Rows per input", "Simple (ms)", "Best radix (ms)", "Best setting", "Ties", "Speedup", "95% CI",
            "All runs faster"]]
    for n in sizes:
        b = best[n]
        lo, hi = speedup_ci[n]
        tbl.append([f"{n:,}", R.fmt(tot[simple_key[n]]["median"], 1), R.fmt(tot[b]["median"], 1), setting(b),
                    len(ties[n]), f"{speedup[n]:.2f}x", f"{lo:.2f}–{hi:.2f}", "yes" if separated[n] else "no"])
    rep.table(tbl, [66, 56, 66, 84, 32, 50, 62, 56], align="rrrlrrrl",
              note=f"Medians of {repeats} runs. Speedup = simple / best radix; 95% CI from a bootstrap over the "
                   "recorded runs. Ties: other radix settings whose runs overlap with the best one's. All runs "
                   "faster: every best-radix run beat every simple run.")
    rep.p(f"The simple join's cost per row grows from {per_row(simple_key[sizes[0]]):.0f} ns to "
          f"{per_row(simple_key[biggest]):.0f} ns as its table outgrows the caches, although the work per row stays "
          f"the same. The radix join stays between {min(per_row(best[n]) for n in sizes):.0f} and "
          f"{max(per_row(best[n]) for n in sizes):.0f} ns per row. This is the paper's central claim: the join is "
          "limited by memory access, and keeping each small table in cache removes most of that cost.")

    rep.h("Where the time goes")
    rep.figure(fig2, "Figure 2. Mean phase times per input row for the simple join and the best radix setting. "
                     "Partitioning is extra work the simple join does not do; much cheaper build and probe phases "
                     "pay for it.")
    m, s = mean[bk], mean[simple_key[biggest]]
    rep.p(f"At {rows_label(biggest)} rows the simple join spends {s['probe_ms']:.0f} ms probing and "
          f"{s['build_ms']:.0f} ms building (means). The radix join spends {m['partition_ms']:.0f} ms partitioning both "
          f"inputs, then {m['build_ms']:.0f} ms building and {m['probe_ms']:.0f} ms probing: probing is "
          f"{s['probe_ms'] / m['probe_ms']:.1f} times and building {s['build_ms'] / m['build_ms']:.1f} times as fast.")

    rep.h("Choosing the number of radix bits")
    rep.figure(fig3, f"Figure 3. Total time against radix bits at {rows_label(biggest)} rows per input (medians with "
                     "interquartile bands), the cost model's prediction (dashed) and the simple join (flat line).")
    worst = max(radix_keys[biggest], key=lambda k: tot[k]["median"])
    tie_text = (f"{len(ties[biggest])} other setting{'s' if len(ties[biggest]) != 1 else ''} "
                f"({', '.join(setting(k) for k in sorted(ties[biggest], key=lambda k: tot[k]['median']))}) "
                "overlapped with it and should be treated as equally good. " if ties[biggest] else
                "No other setting overlapped with it. ")
    rep.p(f"Too few bits leave tables that do not fit in cache; too many make partitioning expensive. At "
          f"{rows_label(biggest)} rows the fastest median was {setting(bk)}, with partition tables of about "
          f"{R.human_bytes(table_bytes(biggest >> bk[2]))}. {tie_text}"
          f"The slowest radix setting ({setting(worst)}) took {tot[worst]['median']:.0f} ms"
          + (", slower than the simple join: one pass on so few bits costs a full copy but leaves tables far larger "
             "than the caches." if tot[worst]["median"] > tot[simple_key[biggest]]["median"] else "."))

    rep.h("A small cost model")
    rep.p("The paper predicts join cost by estimating how many accesses miss each cache level, from the size of the "
          "data each phase touches and the size of each cache, and multiplying by each level's miss cost. We built a "
          "simplified version with two parts.")
    rep.bullets([
        f"<b>Partitioning:</b> the per-record cost of one pass with b bits, taken from {partition_source}. A P-pass "
        "setting costs the sum over its passes, for both R and S.",
        "<b>Build and probe:</b> a base cost plus one penalty per cache level, weighted by the share of table accesses "
        "expected to miss that level, max(0, 1 - cache size / table size). The penalties were fitted with "
        "non-negative least squares to every measured setting, like calibrating the paper's model on this machine.",
    ])
    rep.figure(fig4, "Figure 4. Build plus probe cost per row against the size of one partition's hash table, for "
                     "every setting (medians), with the fitted model. Dashed lines: cache sizes.")
    names = ["base"] + [nm for nm, _ in levels]
    rep.p("Fitted values, in ns per row: " + ", ".join(f"{nm} {c:.1f}" for nm, c in zip(names, coef)) +
          ". Each penalty is the extra cost per row when nearly every table access misses that level. The model's "
          f"total is within {R.pct(max(model_err_simple))} of the measured simple join at every size and within "
          f"{R.pct(max(model_err_best))} of the best radix join.")
    pts = sorted((table_bytes(int(round(k[0] / (1 << k[2])))), (v["build_ms"] + v["probe_ms"]) * 1e6 / k[0])
                 for k, v in med.items())
    rise = next((sz for sz, v in pts if v > 1.5 * coef[0] and all(w > 1.5 * coef[0] for s2, w in pts if s2 >= sz)),
                None)
    if rise and l3 and rise < l3 / 4:
        rep.p(f"The measured cost starts to climb for tables of about {R.human_bytes(rise)}, far below the nominal "
              f"{R.human_bytes(l3)} L3. On this virtual machine the L3 is shared with other tenants and spread over "
              "many slices, so one core effectively gets much less of it, at a higher latency. The fitted L2 penalty "
              "therefore also absorbs part of the L3 behavior.")
    tbl = [["Rows per input", "Fastest median", "Model's choice", "Model choice slower by", "Within noise",
            "Fit-in-L2 rule"]]
    for n in sizes:
        tbl.append([f"{n:,}", setting(best[n]), setting(model_best[n]), R.pct(regret[n], 1),
                    "yes" if model_tied[n] else "no",
                    f"{rule_l2[n]} bit{'s' if rule_l2[n] != 1 else ''}" if rule_l2[n] else "-"])
    rep.table(tbl, [70, 88, 88, 78, 60, 70],
              note="Within noise: the runs of the model's choice overlap with the runs of the fastest setting. "
                   "Fit-in-L2 rule: the smallest B that makes one partition's table smaller than L2.")
    misses = [n for n in sizes if not model_tied[n]]
    if misses:
        parts = []
        for n in misses:
            k = model_best[n]
            parts.append(f"at {rows_label(n)} rows it expected {partition_model(n, k[2], k[3]):.1f} ns per row for "
                         f"partitioning with {k[2]} bits and measured {med[k]['partition_ms'] * 1e6 / n:.1f}")
        rep.p(f"The model's choice was clearly slower at {len(misses)} of {len(sizes)} sizes. There the error came "
              "mostly from the partitioning estimate: " + "; ".join(parts) + ". Neighbouring bit counts are often "
              "close, so a small error in one part of the model can change its pick.")

    if args.seed_results:
        rep.h("Other data seeds")
        tbl = [["Seed", "Rows per input", "Simple (ms)", "Best radix (ms)", "Best setting", "Speedup", "95% CI"]]
        for folder in args.seed_results:
            senv = R.read_environment(folder)
            sruns = defaultdict(list)
            for r in R.read_csv(folder / "timings.csv"):
                assert int(r["matches"]) == (int(r["rows_per_input"]) + 1) // 2
                sruns[int(r["rows_per_input"]), r["algorithm"], int(r["total_bits"]), int(r["passes"])].append(
                    float(r["total_ms"]))
            for n2 in sorted({k[0] for k in sruns}):
                sk = (n2, "simple", 0, 0)
                rk = min((k for k in sruns if k[0] == n2 and k[1] == "radix"),
                         key=lambda k: R.summarize(sruns[k])["median"])
                sm, rm = R.summarize(sruns[sk])["median"], R.summarize(sruns[rk])["median"]
                lo, hi = R.bootstrap_ratio_ci(sruns[sk], sruns[rk])
                tbl.append([senv.get("seed", "?"), f"{n2:,}", R.fmt(sm, 1), R.fmt(rm, 1), setting(rk),
                            f"{sm / rm:.2f}x", f"{lo:.2f}–{hi:.2f}"])
        rep.p("The main runs use data seed 42, like Week 2. To check that the result does not depend on one particular "
              "shuffle of the inputs, we repeated a smaller grid of settings with other seeds.")
        rep.table(tbl, [40, 76, 66, 76, 90, 56, 70], align="rrrrlrr",
                  note="Same keys, different order of rows. Fewer settings and runs than the main experiment.")

    rep.h("Limitations")
    hw = R.counters_available(env)
    lim = [f"<b>Hardware counters</b> were {'available: ' + ', '.join(hw) if hw else 'not available here'}. The cache "
           "explanation rests on timings and table sizes, not on measured miss counts.",
           f"<b>{repeats} runs per setting.</b> Enough to separate the two joins, but neighbouring radix settings often "
           "tie, so the 'best setting' is a range rather than one value."]
    if R.is_virtual_machine(args.results):
        lim.append("<b>Virtual machine.</b> The large L3 is shared with other tenants, so the usable L3 is smaller and "
                   "less predictable than its nominal size.")
    lim += ["<b>Uniform keys.</b> Duplicate-heavy or skewed keys make some partitions larger and would favor more "
            "bits or special handling of large partitions.",
            "<b>Counted output.</b> Matches are counted, not written out. Writing a result table would add the same "
            "cost to both joins."]
    rep.bullets(lim)
    rep.h("Rerun")
    rep.code("make\n./bin/radix_join --test\n./bin/radix_join results/04-radix-join-new\n"
             "python3 analysis/analyze_radix_join.py results/04-radix-join-new")
    rep.references([R.PAPER + " Sections 4 and 5."])
    out = rep.build(args.pdf)
    print(f"Wrote {out}, {args.results / 'summary.csv'} and figures/")


if __name__ == "__main__":
    main()
