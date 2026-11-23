"""Experiment 6 analysis: how many buckets, and which hash function.

Usage: python3 analysis/analyze_bucket_sweep.py results/06-bucket-sweep
           [--hash-results results/06-hash-functions] [--pdf reports/06-bucket-sweep.pdf]
Writes summary.csv and figures/ into the results folders and the PDF report.
Works for any grid the program was run with (the live tool runs small ones).
"""
import argparse
import math
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

import report_lib as R

WEEK2, QUARTER = 0.5, 4.0  # rows per bucket of the Week 2 rule and of commit d28429e
PHASE = [("partition", "partition R and S", R.SERIES[2]), ("build", "build", R.SERIES[0]),
         ("probe", "probe", R.SERIES[1])]
JOIN_COLOR = {"simple": R.SERIES[0], "radix": R.SERIES[1]}
HASH_ORDER = ["identity", "multiply-low", "fibonacci", "crc32", "mix"]
KEY_TEXT = {"dense": "dense keys 0, 1, 2, ...", "stride64": "keys 0, 64, 128, ... (multiples of 64)",
            "random": "random-looking unique keys"}


def load_label(x):
    """0.0625 -> 1/16, 0.5 -> 1/2, 4 -> 4."""
    if x >= 1:
        return f"{x:g}"
    inverse = 1 / x
    return f"1/{inverse:g}" if abs(inverse - round(inverse)) < 1e-9 else f"{x:g}"


def rows_label(n):
    for unit, size in (("M", 1 << 20), ("K", 1 << 10)):
        if n >= size and n % size == 0:
            return f"{n // size:,}{unit}" if unit == "K" else f"{n / size:g}M"
    return f"{n:,}"


def read(folder):
    env = R.read_environment(folder)
    runs = defaultdict(list)
    for r in R.read_csv(folder / "timings.csv"):
        n = int(r["rows_per_input"])
        assert int(r["matches"]) == (n + 1) // 2, "wrong match count"
        key = (n, r["join"], r["hash"], r["keys"], float(r["rows_per_bucket"]))
        runs[key].append({f: float(r[f + "_ms"]) for f in ("partition", "build", "probe", "join", "total")})
    assert all(len(v) == int(env["repeats"]) for v in runs.values()), "missing runs"
    spread = {}
    for r in R.read_csv(folder / "distribution.csv"):
        key = (int(r["rows_per_input"]), r["join"], r["hash"], r["keys"], float(r["rows_per_bucket"]))
        spread[key] = {k: float(v) for k, v in r.items() if k not in ("join", "hash", "keys")}
    bits = {k[0]: int(r["bits"]) for r in R.read_csv(folder / "timings.csv") for k in [(int(r["rows_per_input"]),)]
            if r["join"] == "radix"}
    return env, runs, spread, bits


def stats(runs):
    tot = {k: R.summarize([v["total"] for v in vs]) for k, vs in runs.items()}
    # Phase medians: charts and text use medians throughout (no stacked bars here).
    mean = {k: {f: float(np.median([v[f] for v in vs])) for f in ("partition", "build", "probe", "total")}
            for k, vs in runs.items()}
    return tot, mean


def write_summary(folder, runs, tot, mean, spread):
    rows = []
    for k in sorted(runs):
        n, join, hash_, keys, load = k
        t, sp = tot[k], spread.get(k, {})
        rows.append({"rows_per_input": n, "join": join, "hash": hash_, "keys": keys, "rows_per_bucket": load,
                     "runs": t["n"], "total_median_ms": round(t["median"], 3), "total_min_ms": round(t["min"], 3),
                     "total_max_ms": round(t["max"], 3), "total_cv_percent": round(100 * t["cv"], 2),
                     "ns_per_input_row": round(t["median"] * 1e6 / n, 3),
                     "partition_median_ms": round(mean[k]["partition"], 3), "build_median_ms": round(mean[k]["build"], 3),
                     "probe_median_ms": round(mean[k]["probe"], 3),
                     "actual_rows_per_bucket": sp.get("actual_rows_per_bucket", ""),
                     "empty_bucket_share": sp.get("empty_bucket_share", ""),
                     "longest_chain": int(sp["longest_chain"]) if sp else "",
                     "rows_checked_per_probe": sp.get("rows_checked_per_probe", ""),
                     "largest_partition_rows": int(sp["largest_partition_rows"]) if sp else ""})
    R.write_summary(folder / "summary.csv", rows)


def ratio_text(x):
    return f"{x:.2f}x" if x < 10 else f"{x:.0f}x"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, nargs="?", default=R.ROOT / "results" / "06-bucket-sweep")
    parser.add_argument("--hash-results", type=Path, default=None,
                        help="folder of a run with several hash functions and key patterns")
    parser.add_argument("--pdf", type=Path, default=R.REPORTS / "06-bucket-sweep.pdf")
    args = parser.parse_args()

    env, runs, spread, bits = read(args.results)
    tot, mean = stats(runs)
    write_summary(args.results, runs, tot, mean, spread)
    repeats = int(env["repeats"])

    # ---- part 1: rows per bucket, one hash and one key pattern ---------------------
    hashes = sorted({k[2] for k in runs}, key=HASH_ORDER.index)
    patterns = sorted({k[3] for k in runs})
    H0 = "mix" if "mix" in hashes else hashes[0]
    K0 = "dense" if "dense" in patterns else patterns[0]
    part1 = {k: v for k, v in tot.items() if k[2] == H0 and k[3] == K0}
    sizes = sorted({k[0] for k in part1})
    joins = [j for j in ("simple", "radix") if any(k[1] == j for k in part1)]
    loads = sorted({k[4] for k in part1})
    key = lambda n, j, l: (n, j, H0, K0, l)
    have = lambda n, j, l: key(n, j, l) in part1
    best, ties, ratio = {}, {}, {}
    for n in sizes:
        for j in joins:
            ls = [l for l in loads if have(n, j, l)]
            if not ls:
                continue
            b = min(ls, key=lambda l: part1[key(n, j, l)]["median"])
            best[n, j] = b
            ties[n, j] = [l for l in ls if l != b and part1[key(n, j, l)]["min"] <= part1[key(n, j, b)]["max"]]
            for rule in (WEEK2, QUARTER):
                if have(n, j, rule):
                    raw = lambda l: [v["total"] for v in runs[key(n, j, l)]]
                    ratio[n, j, rule] = (part1[key(n, j, rule)]["median"] / part1[key(n, j, b)]["median"],
                                         R.bootstrap_ratio_ci(raw(rule), raw(b)))
    biggest = sizes[-1]
    main_join = "simple" if "simple" in joins else joins[0]
    typical_cv = sorted(t["cv"] for t in tot.values())[len(tot) // 2]

    # ---- part 2: hash functions and key patterns ------------------------------------
    hfolder = args.hash_results
    if hfolder is None and (len(hashes) > 1 or len(patterns) > 1):
        hfolder = args.results
    hpart = None
    if hfolder is not None:
        if hfolder == args.results:
            h_env, h_runs, h_spread, h_tot, h_mean, h_bits = env, runs, spread, tot, mean, bits
        else:
            h_env, h_runs, h_spread, h_bits = read(hfolder)
            h_tot, h_mean = stats(h_runs)
            write_summary(hfolder, h_runs, h_tot, h_mean, h_spread)
        h_n = max(k[0] for k in h_tot)
        h_load = WEEK2 if any(k[4] == WEEK2 for k in h_tot) else min(k[4] for k in h_tot)
        h_keys = {k: v for k, v in h_tot.items() if k[0] == h_n and k[4] == h_load}
        hpart = {"n": h_n, "load": h_load, "tot": h_keys, "spread": h_spread, "repeats": int(h_env["repeats"]),
                 "hashes": sorted({k[2] for k in h_keys}, key=HASH_ORDER.index),
                 "patterns": [p for p in ("dense", "stride64", "random") if any(k[3] == p for k in h_keys)],
                 "joins": [j for j in ("simple", "radix") if any(k[1] == j for k in h_keys)],
                 "loads": sorted({k[4] for k in h_tot if k[0] == h_n}), "bits": h_bits.get(h_n)}

    # ---- figures -------------------------------------------------------------------
    R.style_charts()
    ramp = plt.get_cmap("Blues")
    size_color = {n: ramp(0.45 + 0.5 * i / max(1, len(sizes) - 1)) for i, n in enumerate(sizes)}
    fig, axes = R.figure(2.75, ncols=len(joins), sharey=True)
    axes = np.atleast_1d(axes)
    top = 1.0
    for ax, j in zip(axes, joins):
        for n in sizes:
            ls = [l for l in loads if have(n, j, l)]
            if not ls or (n, j) not in best:
                continue
            base = part1[key(n, j, best[n, j])]["median"]
            ys = [part1[key(n, j, l)]["median"] / base for l in ls]
            top = max(top, max(ys))
            ax.plot(ls, ys, "o-", color=size_color[n], label=f"{rows_label(n)} rows")
        for rule, text in ((WEEK2, "Week 2"), (QUARTER, "d28429e")):
            if loads[0] <= rule <= loads[-1]:
                ax.axvline(rule, color=R.INK_3, lw=0.8, ls=(0, (2, 2)), zorder=0)
                ax.text(rule * 1.08, 0.98, text, transform=ax.get_xaxis_transform(), fontsize=7, color=R.INK_3,
                        va="top")
        ax.set_xscale("log", base=2)
        ax.set_xticks(loads, [load_label(l) for l in loads])
        ax.minorticks_off()
        ax.set(title=f"{j} hash join", xlabel="rows per bucket (log scale)")
    axes[0].set_yscale("log", base=2)
    ticks = [t for t in (1, 1.25, 1.5, 2, 3, 4, 8, 16, 32, 64) if t <= top * 1.05]
    axes[0].set_yticks(ticks, [f"{t:g}x" for t in ticks])
    axes[0].minorticks_off()
    axes[0].set_ylabel("time / fastest setting")
    axes[-1].legend(loc="upper left", fontsize=7.2, bbox_to_anchor=(0.02, 0.9))
    fig1 = R.save(fig, args.results, "01_rows_per_bucket.png")

    fig, axes = R.figure(2.6, ncols=len(joins), sharey=True)
    axes = np.atleast_1d(axes)
    lowest, highest = float("inf"), 0.0
    for ax, j in zip(axes, joins):
        ls = [l for l in loads if have(biggest, j, l)]
        for field, label, color in PHASE + [("total", "total", R.INK)]:
            if j == "simple" and field == "partition":
                continue
            vals = [mean[key(biggest, j, l)][field] * 1e6 / biggest for l in ls]
            lowest, highest = min(lowest, min(vals)), max(highest, max(vals))
            ax.plot(ls, vals, "o-", color=color, label=label, lw=2.2 if field == "total" else 1.5)
        ax.set_xscale("log", base=2)
        ax.set_xticks(ls, [load_label(l) for l in ls])
        ax.minorticks_off()
        ax.set(title=f"{j} hash join, {rows_label(biggest)} rows", xlabel="rows per bucket (log scale)")
        ax.legend(loc="upper left", fontsize=7.2)
    axes[0].set_yscale("log")
    yt = [t for t in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000) if lowest * 0.7 <= t <= highest * 1.4]
    axes[0].set_yticks(yt, [f"{t:g}" for t in yt])
    axes[0].minorticks_off()
    axes[0].set_ylabel("median ns per input row (log scale)")
    fig2 = R.save(fig, args.results, "02_build_and_probe.png")

    # Spread of the simple join's table against a random hash.
    sp_keys = [k for k in part1 if k[1] == "simple" and k in spread]
    fig3 = None
    if sp_keys:
        fig, axes = R.figure(2.5, ncols=2)
        alpha = np.geomspace(min(spread[k]["actual_rows_per_bucket"] for k in sp_keys) * 0.8,
                             max(spread[k]["actual_rows_per_bucket"] for k in sp_keys) * 1.25, 100)
        axes[0].plot(alpha, alpha + 0.5, color=R.INK_3, lw=1, ls=(0, (3, 2)), label="random hash, predicted")
        axes[1].plot(alpha, np.exp(-alpha), color=R.INK_3, lw=1, ls=(0, (3, 2)), label="random hash, predicted")
        for n in sizes:
            ks = sorted((k for k in sp_keys if k[0] == n), key=lambda k: k[4])
            if not ks:
                continue
            xs = [spread[k]["actual_rows_per_bucket"] for k in ks]
            axes[0].plot(xs, [spread[k]["rows_checked_per_probe"] for k in ks], "o", color=size_color[n],
                         label=f"{rows_label(n)} rows, measured")
            axes[1].plot(xs, [spread[k]["empty_bucket_share"] for k in ks], "o", color=size_color[n])
        for ax in axes:
            ax.set_xscale("log", base=2)
            ax.set_xticks(loads, [load_label(l) for l in loads])
            ax.minorticks_off()
            ax.set_xlabel("rows per bucket (log scale)")
        axes[0].set_yscale("log", base=2)
        yt = [t for t in (0.5, 1, 2, 4, 8) if t <= (alpha[-1] + 0.5) * 1.1]
        axes[0].set_yticks(yt, [f"{t:g}" for t in yt])
        axes[0].set(title="rows checked per probe", ylabel="rows")
        axes[1].set(title="share of empty buckets", ylim=(0, 1))
        axes[1].yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{100 * v:.0f}%"))
        axes[0].legend(loc="upper left", fontsize=6.8)
        fig3 = R.save(fig, args.results, "03_spread_vs_random.png")

    fig4 = None
    if hpart and len(hpart["hashes"]) > 1:
        fig, axes = R.figure(2.3, ncols=len(hpart["patterns"]), sharey=True)
        axes = np.atleast_1d(axes)
        hs = hpart["hashes"]
        width = 0.8 / len(hpart["joins"])
        for ax, p in zip(axes, hpart["patterns"]):
            for i, j in enumerate(hpart["joins"]):
                vals = [hpart["tot"].get((hpart["n"], j, h, p, hpart["load"]), {}).get("median", np.nan)
                        * 1e6 / hpart["n"] for h in hs]
                ax.plot(vals, np.arange(len(hs)) + (i - (len(hpart["joins"]) - 1) / 2) * width * 0.8, "o",
                        color=JOIN_COLOR[j], markersize=6, label=f"{j} join")
            ax.set_xscale("log")
            lo_x, hi_x = ax.get_xlim()
            xt = [t for t in (1, 2, 3, 5, 10, 20, 30, 50, 100, 200, 300, 500, 1000, 2000) if lo_x <= t <= hi_x]
            ax.set_xticks(xt, [f"{t:g}" for t in xt])
            ax.minorticks_off()
            ax.set(title=p, xlabel="ns per input row (log scale)")
            ax.grid(axis="y", visible=True)
            ax.set_ylim(len(hs) - 0.5, -0.5)
        axes[0].set_yticks(np.arange(len(hs)), hs)
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper right", ncol=len(labels), fontsize=7.2, bbox_to_anchor=(1, 1.06))
        src = hfolder
        fig4 = R.save(fig, src, "04_hash_functions.png")

    # ---- report --------------------------------------------------------------------
    rep = R.Report("How many buckets, and which hash function",
                   f"Experiment 6  ·  {R.cpu_model(args.results)}  ·  {R.machine_line(env)}")
    bj = best.get((biggest, main_join))
    w2 = ratio.get((biggest, main_join, WEEK2))
    q4 = ratio.get((biggest, main_join, QUARTER))
    summary = (f"We never chose the bucket count with an experiment: Week 2 used {load_label(WEEK2)} rows per bucket "
               f"and commit d28429e changed it to {load_label(QUARTER)}, both without a measurement. Here the "
               f"{main_join} join at {biggest:,} rows per input was fastest with {load_label(bj)} rows per bucket")
    if (biggest, "radix") in best and main_join != "radix":
        summary += f", the radix join with {load_label(best[biggest, 'radix'])}"
    summary += ". "
    if w2:
        summary += (f"The Week 2 rule was {ratio_text(w2[0])} as slow as the fastest setting"
                    + (f" and the d28429e rule {ratio_text(q4[0])}" if q4 else "") + ". ")
    if hpart and len(hpart["hashes"]) > 1 and "mix" in hpart["hashes"]:
        # M5 copy: every claim here is computed from this run's hash grid, so a live run with other
        # key patterns or hashes gets a sentence that matches its own data.
        hj = "simple" if "simple" in hpart["joins"] else hpart["joins"][0]
        at = lambda h, p: hpart["tot"].get((hpart["n"], hj, h, p, hpart["load"]))
        pats = hpart["patterns"]
        mix_sp = [hpart["spread"].get((hpart["n"], "simple", "mix", p, hpart["load"])) for p in pats]
        if all(mix_sp):
            dev = max(abs(float(q["empty_bucket_share"]) - math.exp(-float(q["actual_rows_per_bucket"]))) for q in mix_sp)
            summary += (f"Our hash function, mix, left the share of empty buckets a random hash leaves (within "
                        f"{100 * dev:.2f} percentage points) on {'every tested key pattern' if len(pats) > 1 else 'the tested keys'} "
                        f"({', '.join(pats)}). ")
        vs = [(at(h, p)["median"] / at("mix", p)["median"], h, p) for h in hpart["hashes"] for p in pats
              if h != "mix" and at(h, p) and at("mix", p)]
        if vs:
            fast, slow = min(vs), max(vs)
            if fast[0] < 0.95:
                summary += f"The {fast[1]} hash beat it on {fast[2]} keys ({ratio_text(1 / fast[0])} as fast)"
                summary += (f", but the {slow[1]} hash was {ratio_text(slow[0])} as slow as mix on {slow[2]} keys. "
                            if slow[0] > 1.5 else ", and no hash was more than " f"{ratio_text(slow[0])} as slow as mix. ")
            elif slow[0] > 1.5:
                summary += f"No hash beat it; the {slow[1]} hash was {ratio_text(slow[0])} as slow as mix on {slow[2]} keys. "
    rep.summary(summary)
    tiles = [(load_label(bj), f"fastest rows per bucket, {main_join} join, {rows_label(biggest)} rows")]
    if w2:
        tiles.append((ratio_text(w2[0]), "Week 2 rule against the fastest"))
    if q4:
        tiles.append((ratio_text(q4[0]), "d28429e rule against the fastest"))
    worst = None
    if hpart and len(hpart["hashes"]) > 1:
        mix_base = {(j, p): hpart["tot"].get((hpart["n"], j, "mix", p, hpart["load"])) for j in hpart["joins"]
                    for p in hpart["patterns"]}
        cand = [(v["median"] / mix_base[k[1], k[3]]["median"], k) for k, v in hpart["tot"].items()
                if mix_base.get((k[1], k[3]))]
        if cand:
            worst = max(cand)
            tiles.append((ratio_text(worst[0]), f"worst hash against mix ({worst[1][2]}, {worst[1][3]} keys)"))
    rep.key_numbers(tiles[:4])

    rep.h("The question")
    rep.p("A hash join puts every row of the smaller table R into a hash table: a row's key is turned into a "
          "number by a hash function, and that number picks a bucket. Rows that land in the same bucket are kept in "
          "a chain. To find the matches for a row of S, the join hashes its key and walks the whole chain of that "
          "bucket, comparing keys. So two choices decide how long a chain is: how many buckets the table has, and "
          "how evenly the hash function spreads the keys over them.")
    rep.p("Rows per bucket is rows divided by buckets. With 0.5 rows per bucket there are twice as many buckets as "
          "rows; with 4 there is one bucket for every 4 rows. The bucket count is always a power of two, so the "
          "bucket is just the lowest bits of the hash. Week 2 used 0.5 (\"at least 2 buckets per row\"), and commit "
          "d28429e changed it to 4. Neither was tested, and nobody checked how evenly the hash spreads our keys. "
          "This experiment measures both.")

    rep.h("What we varied")
    hash_list = ", ".join(hashes)
    rep.bullets([
        f"<b>Rows per bucket:</b> {', '.join(load_label(l) for l in loads)}. The table gets the smallest power of "
        "two number of buckets that keeps the average at or below that value.",
        f"<b>Input size:</b> {', '.join(f'{n:,}' for n in sizes)} rows per input, from inputs that fit in the "
        "caches to inputs far larger than them.",
        f"<b>Join:</b> {' and '.join(joins)}. The radix join first splits R and S into partitions "
        + (f"({', '.join(f'{bits[n]} bits at {rows_label(n)}' for n in sizes if n in bits)}, about 4,096 rows per "
           "partition, 1 pass) " if bits else "") +
        "and gives every partition its own small table with the same rows-per-bucket rule.",
        f"<b>Hash function and key pattern</b> in a second run"
        + (f" at {hpart['n']:,} rows (section \"Which hash function\")." if hpart else ".") +
        f" The main sweep used {H0} (the hash of every other experiment) on {KEY_TEXT[K0]}.",
    ])

    rep.h("Method")
    rep.bullets([
        f"<b>Repetitions.</b> One verified warmup, then {repeats} recorded runs of every setting. Each round runs every "
        "setting of one input size once in a newly shuffled order, so slow periods affect all settings alike. One "
        f"thread; {R.pinning_text(env)}.",
        "<b>Timing.</b> All memory is allocated and written before any timer starts. Clearing the bucket array is "
        "build time, so more buckets cost more to build. The radix join's build-and-probe loop is timed once with no "
        "clock reads inside it; its build time comes from a separate build-only pass after each run, and its probe "
        "time is the join time minus that build time.",
        "<b>How evenly the keys spread</b> is measured on the real tables before the timed runs: the share of empty "
        "buckets, the longest chain, and the rows a probe has to compare on average (the length of the chain it "
        "walks). A random hash would leave a share e<super>-a</super> of the buckets empty at a rows per bucket "
        "(Poisson), and a probe would compare a + 0.5 rows on average, because half of the probes find their key.",
        f"<b>Statistics.</b> Medians of the recorded runs in text, tables and charts. Typical run-to-run variation: {R.pct(typical_cv)} of the mean. Two settings count as a tie when their run ranges "
        "overlap. Ratios carry 95% bootstrap confidence intervals.",
        "<b>Correctness.</b> Every run is checked against the known number of matches and a checksum of the matching "
        "row pairs. The --test mode compares every hash function, key pattern and both joins with a nested-loop join "
        "and checks that 0.5 and 4 rows per bucket give exactly the bucket counts of the two old rules.",
    ])

    rep.h("How many buckets")
    rep.figure(fig1, "Figure 1. Time of each setting divided by the fastest setting at the same input size (median of "
                     f"{repeats} runs). 1x is the fastest. Dashed lines: the Week 2 rule and the d28429e rule.")
    tbl = [["Rows", "Join", "Fastest rows/bucket", "Ties", "Fastest (ms)", "Bucket memory", "Week 2 rule",
            "d28429e rule"]]
    hl = []
    for n in sizes:
        for j in joins:
            if (n, j) not in best:
                continue
            b = best[n, j]
            sp = spread.get(key(n, j, b))
            cells = [f"{n:,}", j, load_label(b), ", ".join(load_label(l) for l in ties[n, j]) or "none",
                     R.fmt(part1[key(n, j, b)]["median"], 2),
                     R.human_bytes(4 * sp["buckets"] / (1 if j == "simple" else 2 ** sp["bits"])) if sp else "-"]
            for rule in (WEEK2, QUARTER):
                r = ratio.get((n, j, rule))
                cells.append(f"{r[0]:.2f}x ({r[1][0]:.2f}–{r[1][1]:.2f})" if r else "-")
            tbl.append(cells)
            if n == biggest:
                hl.append(len(tbl) - 1)
    rep.table(tbl, [54, 38, 58, 58, 52, 56, 84, 84], align="llllrrrr",
              note="Rule columns: median time with that rule divided by the fastest median, with a 95% bootstrap "
                   "interval. Ties: other settings whose run ranges overlap the fastest one's. Bucket memory: the "
                   "bucket array at the fastest setting (4 bytes per bucket; for the radix join, one partition's "
                   "table on average).")

    if len(loads) > 1:
        lo, hi = loads[0], loads[-1]
        sj = main_join
        m_lo, m_hi = mean[key(biggest, sj, lo)], mean[key(biggest, sj, hi)]
        rep.p(f"Why there is a best value: at {biggest:,} rows the {sj} join spent {m_lo['build']:.0f} ms building with "
              f"{load_label(lo)} rows per bucket against {m_hi['build']:.0f} ms with {load_label(hi)}, because every "
              f"bucket is cleared before the inserts. Probing went the other way: {m_lo['probe']:.0f} ms against "
              f"{m_hi['probe']:.0f} ms, because each probe walks a chain and every row in the chain is one more "
              "memory read that has to wait for the one before it. The fastest setting is where adding buckets stops "
              "saving more probe time than it costs in build time (Figure 2).")
    rep.figure(fig2, f"Figure 2. Median time per input row at {biggest:,} rows for each phase and in total (log "
                     "scale). Build gets slower with more buckets, probe gets faster.")
    if "radix" in joins and any(k[1] == "radix" for k in spread):
        rk = [k for k in spread if k[1] == "radix" and k[2] == H0 and k[3] == K0 and k[0] == biggest
              and k[4] == WEEK2]
        if rk:
            a = spread[rk[0]]["actual_rows_per_bucket"]
            rep.p(f"In the radix join every partition gets its own table, sized for that partition's rows. Partitions "
                  f"hold about 4,096 rows, a power of two, so a partition with a few more rows than 4,096 gets a "
                  f"table twice as large. At the {load_label(WEEK2)} setting this gave {a:.2f} rows per bucket on "
                  "average, not 0.5. The settings are what the code is told; summary.csv gives the actual values.")

    if fig3:
        rep.h("Does mix spread our keys evenly?")
        dev = max(abs(spread[k]["empty_bucket_share"] - math.exp(-spread[k]["actual_rows_per_bucket"]))
                  for k in sp_keys)
        dev_probe = max(abs(spread[k]["rows_checked_per_probe"] - spread[k]["actual_rows_per_bucket"] - 0.5)
                        for k in sp_keys)
        rep.figure(fig3, f"Figure 3. The simple join's table with {H0} on {KEY_TEXT[K0]}, measured (dots) against "
                         "what a perfectly random hash would give (dashed line).")
        rep.p(f"Across every size and setting, the share of empty buckets was within {100 * dev:.2f} percentage "
              f"points of the random-hash value, and the rows compared per probe within {dev_probe:.3f} rows. So "
              f"{H0} spreads our keys as evenly as a random hash would. That also means the chains are as long as "
              "chance makes them: at 4 rows per bucket a probe compares about 4.5 rows, at 0.5 about 1.")

    if hpart and len(hpart["hashes"]) > 1:
        rep.h("Which hash function")
        rep.p(f"A second run at {hpart['n']:,} rows per input compared {len(hpart['hashes'])} hash functions on "
              f"{len(hpart['patterns'])} key patterns, at {load_label(hpart['load'])} rows per bucket"
              + (f" (and {', '.join(load_label(l) for l in hpart['loads'] if l != hpart['load'])} in summary.csv)"
                 if len(hpart["loads"]) > 1 else "") + ".")
        rep.bullets([
            "<b>identity</b>: the key itself. Costs nothing, but the bucket is just the low bits of the key.",
            "<b>multiply-low</b>: the key times a large odd number, low bits. The low bits of a product depend only "
            "on the low bits of the key, so it fails where identity fails.",
            "<b>fibonacci</b>: the same product, read from the top bits, which depend on every bit of the key "
            "(Knuth's multiplicative hashing).",
            "<b>crc32</b>: the processor's checksum instruction. Cheap, but it mixes bits in a fixed, linear way.",
            "<b>mix</b>: two multiplications and three shifts (the SplitMix64 finalizer); the hash of every other "
            "experiment.",
        ])
        if fig4:
            rep.figure(fig4, f"Figure 4. Median time per input row at {hpart['n']:,} rows and "
                             f"{load_label(hpart['load'])} rows per bucket (log scale).")
        tbl = [["Keys", "Hash", "Empty buckets", "Longest chain", "Rows per probe", "Simple (ms)", "Radix (ms)",
                "Largest partition"]]
        for p in hpart["patterns"]:
            for h in hpart["hashes"]:
                ks = {j: (hpart["n"], j, h, p, hpart["load"]) for j in ("simple", "radix")}
                s_sp = hpart["spread"].get(ks["simple"]) or hpart["spread"].get(ks["radix"])
                r_sp = hpart["spread"].get(ks["radix"])
                tbl.append([p if h == hpart["hashes"][0] else "", h,
                            R.pct(s_sp["empty_bucket_share"], 1) if s_sp else "-",
                            f"{s_sp['longest_chain']:.0f}" if s_sp else "-",
                            f"{s_sp['rows_checked_per_probe']:.2f}" if s_sp else "-",
                            R.fmt(hpart["tot"][ks["simple"]]["median"], 1) if ks["simple"] in hpart["tot"] else "-",
                            R.fmt(hpart["tot"][ks["radix"]]["median"], 1) if ks["radix"] in hpart["tot"] else "-",
                            f"{r_sp['largest_partition_rows']:,.0f}" if r_sp else "-"])
        rep.table(tbl, [52, 64, 56, 52, 56, 56, 52, 70], align="llrrrrrr",
                  note="Spread columns describe the simple join's table. Largest partition: rows of R in the radix "
                       "join's largest partition" + (f" ({hpart['bits']} bits); an even split gives "
                       f"{hpart['n'] / 2 ** hpart['bits']:,.0f} rows per partition." if hpart["bits"] else "."))
        text = []
        for p in hpart["patterns"]:
            per = {h: hpart["tot"].get((hpart["n"], main_join, h, p, hpart["load"])) for h in hpart["hashes"]}
            per = {h: v for h, v in per.items() if v}
            if not per:
                continue
            fast = min(per, key=lambda h: per[h]["median"])
            slow = max(per, key=lambda h: per[h]["median"])
            text.append(f"{KEY_TEXT[p]}: fastest {fast} ({per[fast]['median']:.1f} ms), slowest {slow} "
                        f"({per[slow]['median']:.1f} ms, {per[slow]['median'] / per[fast]['median']:.1f}x)")
        rep.p(f"For the {main_join} join: " + "; ".join(text) + ".")
        rep.p("Reading the table: a hash that uses only the low bits of the key is perfect for dense keys (every bucket "
              "gets the same number of rows, so no chain is longer than it has to be), and it costs nothing to compute. "
              "But keys that are multiples of 64 have their 6 lowest bits always 0, so those hashes use only 1 bucket "
              "in 64 and every chain is 64 times longer.")
        g = lambda j, h, p: hpart["tot"].get((hpart["n"], j, h, p, hpart["load"]))
        sp_ = lambda j, h, p: hpart["spread"].get((hpart["n"], j, h, p, hpart["load"]))
        extra = []
        if g("simple", "identity", "dense") and g("simple", "mix", "dense"):
            d = (g("simple", "mix", "dense")["median"] - g("simple", "identity", "dense")["median"]) * 1e6 / hpart["n"]
            extra.append(f"On dense keys mix cost {d:.1f} ns more per row than identity in the simple join "
                         f"({g('simple', 'mix', 'dense')['median']:.1f} against {g('simple', 'identity', 'dense')['median']:.1f} ms): "
                         "it computes a real hash, and it leaves the share of empty buckets a random hash leaves, so a "
                         "probe compares about 1 row instead of 0.5.")
        if g("radix", "identity", "stride64") and g("radix", "mix", "stride64") and sp_("radix", "identity", "stride64"):
            q = sp_("radix", "identity", "stride64")
            slower = g("radix", "identity", "stride64")["median"] / g("radix", "mix", "stride64")["median"]
            extra.append(f"The radix join takes its partition from the same low bits, so with identity on multiples of 64 "
                         f"only {q['partitions_used']:.0f} of {q['partitions']:.0f} partitions got rows (the largest "
                         f"{q['largest_partition_rows']:,.0f} rows instead of about {hpart['n'] / q['partitions']:,.0f}). "
                         + (f"At this size even those big partitions fit in the cache, so it was not slower "
                            f"({g('radix', 'identity', 'stride64')['median']:.1f} against "
                            f"{g('radix', 'mix', 'stride64')['median']:.1f} ms for mix); at larger inputs they would not fit."
                            if slower < 1.1 else f"That made it {slower:.1f}x as slow as with mix."))
        worst_of = {}
        for h in hpart["hashes"]:
            rs = []
            for p in hpart["patterns"]:
                per = {x: g(main_join, x, p) for x in hpart["hashes"] if g(main_join, x, p)}
                if h in per:
                    rs.append(per[h]["median"] / min(v["median"] for v in per.values()))
            if rs:
                worst_of[h] = max(rs)
        if "mix" in worst_of and len(worst_of) > 1:
            riskiest = max(worst_of, key=worst_of.get)
            extra.append(f"Worst case over the {len(hpart['patterns'])} key patterns ({main_join} join): mix took at most "
                         f"{worst_of['mix']:.1f}x as long as the fastest hash for that pattern, {riskiest} up to "
                         f"{worst_of[riskiest]:.1f}x. That is the trade-off: mix gives up speed on easy keys so that no "
                         "key pattern can make it badly slow.")
        rep.p(" ".join(extra))

    rep.h("What this answers")
    answer = []
    for j in joins:
        bs = {n: best[n, j] for n in sizes if (n, j) in best}
        if bs:
            answer.append(f"<b>{j} join:</b> fastest at " + ", ".join(f"{load_label(b)} ({rows_label(n)} rows)"
                                                                        for n, b in bs.items()) + ".")
    for j in joins:
        ns = [n for n in sizes if (n, j) in best]
        ok = [l for l in loads if all(l == best[n, j] or l in ties[n, j] for n in ns)]
        if ok:
            answer.append(f"<b>Rule for the {j} join on this machine:</b> {load_label(max(ok))} rows per bucket. It is "
                          "the fewest buckets (least memory) that tied with the fastest setting at every size tested.")
        else:
            worst_ratio = {l: max(part1[key(n, j, l)]["median"] / part1[key(n, j, best[n, j])]["median"]
                                  for n in ns if have(n, j, l)) for l in loads}
            pick = min(worst_ratio, key=worst_ratio.get)
            answer.append(f"<b>Rule for the {j} join on this machine:</b> no single value tied with the fastest at "
                          f"every size. {load_label(pick)} rows per bucket came closest: at most "
                          f"{worst_ratio[pick]:.2f}x the fastest at any size.")
    rule_simple = None
    if "simple" in joins:
        ok = [l for l in loads if all(l == best[n, "simple"] or l in ties[n, "simple"] for n in sizes if (n, "simple") in best)]
        rule_simple = max(ok) if ok else None
    if rule_simple and have(biggest, "simple", WEEK2) and rule_simple != WEEK2:
        mem = lambda l: R.human_bytes(4 * spread[key(biggest, "simple", l)]["buckets"])
        answer.append(f"<b>Memory cost:</b> at {biggest:,} rows the bucket array takes {mem(rule_simple)} with "
                      f"{load_label(rule_simple)} rows per bucket, against {mem(WEEK2)} with the Week 2 rule"
                      + (f" and {mem(QUARTER)} with the d28429e rule" if have(biggest, "simple", QUARTER) else "")
                      + ". Speed is bought with memory; the right rule depends on how much memory the join may use.")
    both = [(n, j) for n in sizes for j in joins if (n, j, WEEK2) in ratio and (n, j, QUARTER) in ratio]
    if both:
        slower = sum(ratio[n, j, QUARTER][0] > ratio[n, j, WEEK2][0] for n, j in both)
        answer.append(f"<b>Old rules:</b> the d28429e rule (4 rows per bucket) was slower than the Week 2 rule in "
                      f"{slower} of {len(both)} size-and-join combinations, from "
                      f"{min(ratio[k + (QUARTER,)][0] / ratio[k + (WEEK2,)][0] for k in both):.2f}x to "
                      f"{max(ratio[k + (QUARTER,)][0] / ratio[k + (WEEK2,)][0] for k in both):.2f}x as slow. "
                      "Reports 2 and 2b measure the same two rules with the Week 2 program.")
    rep.bullets(answer)

    rep.h("Limitations")
    rep.bullets([
        "<b>One table design.</b> Chained buckets with a separate next array, power-of-two bucket counts. Open "
        "addressing, buckets that hold several keys, or prime bucket counts with a remainder would give other "
        "optimal values.",
        "<b>Unique keys.</b> Every left key appears once. Duplicate or skewed keys make chains longer for reasons "
        "the bucket count cannot fix.",
        "<b>One machine, one thread.</b> The best value depends on cache and memory sizes.",
        "<b>Power-of-two rounding.</b> A setting gives an actual rows-per-bucket value between half of it and "
        "itself, depending on the row count; summary.csv lists the actual values.",
    ])
    rep.h("Rerun")
    rep.code("make\n./bin/bucket_sweep --test\n./bin/bucket_sweep results/06-bucket-sweep-new\n"
             "./bin/bucket_sweep results/06-hash-functions-new --sizes 1048576 --loads 0.5,4 \\\n"
             "    --hashes identity,multiply-low,fibonacci,crc32,mix --keys dense,stride64,random\n"
             "python3 analysis/analyze_bucket_sweep.py results/06-bucket-sweep-new \\\n"
             "    --hash-results results/06-hash-functions-new")
    out = rep.build(args.pdf)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
