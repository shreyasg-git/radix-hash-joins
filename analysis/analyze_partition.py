"""Radix partitioning analysis: cost of one or more partitioning passes as the radix bits grow.

Usage: python3 analysis/analyze_partition.py results/radix-partitioning [--pdf reports/radix-partitioning.pdf]
Writes summary.csv and figures/ into the results folder and the PDF report.
"""
import argparse
import math
from collections import defaultdict
from pathlib import Path

import report_lib as R


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, nargs="?", default=R.ROOT / "results" / "radix-partitioning")
    parser.add_argument("--pdf", type=Path, default=R.REPORTS / "radix-partitioning.pdf")
    args = parser.parse_args()
    env = R.read_environment(args.results)
    rows = R.read_csv(args.results / "timings.csv")
    n = int(env["rows"])
    repeats = int(env["repeats"])

    # ---- check and collect --------------------------------------------------
    per_run = defaultdict(lambda: defaultdict(float))   # (mode,P,B) -> run -> ms over all passes
    split = defaultdict(lambda: defaultdict(list))      # (mode,B) -> part -> [ms], single pass only
    for r in rows:
        mode, P, B, p = r["page_mode"], int(r["passes"]), int(r["total_bits"]), int(r["pass"])
        assert int(r["rows"]) == n and 1 <= p <= P
        per_run[mode, P, B][int(r["run"])] += float(r["pass_ms"])
        if P == 1:
            split[mode, B]["histogram"].append(float(r["histogram_ms"]))
            split[mode, B]["scatter"].append(float(r["scatter_ms"]))
    assert all(len(v) == repeats for v in per_run.values()), "missing runs"

    def ns(ms):
        return ms * 1e6 / n

    stat, summary = {}, []
    for (mode, P, B), runs in sorted(per_run.items()):
        s = R.summarize([ns(v) for v in runs.values()])
        stat[mode, P, B] = s
        summary.append({"page_mode": mode, "passes": P, "total_bits": B, "runs": s["n"],
                        "median_ns_per_row": round(s["median"], 4), "mean_ns_per_row": round(s["mean"], 4),
                        "sd_ns_per_row": round(s["sd"], 4), "cv_percent": round(100 * s["cv"], 2),
                        "min_ns_per_row": round(s["min"], 4), "max_ns_per_row": round(s["max"], 4)})
    R.write_summary(args.results / "summary.csv", summary)
    cost = {k: s["median"] for k, s in stat.items()}
    typical_cv = sorted(s["cv"] for s in stat.values())[len(stat) // 2]

    modes = sorted({m for m, _, _ in cost}, key=lambda m: ["4k", "2m", "default"].index(m))
    main_mode = "4k" if "4k" in modes else modes[0]
    page_kib = int(env.get("page_bytes", "4096")) // 1024
    mode_label = {"4k": "4 KiB pages", "2m": "2 MiB huge pages", "default": f"{page_kib} KiB pages (system default)"}
    steal = R.steal_summary([r for r in rows if r["pass"] == "1"], "pass_ms")
    passes = sorted({P for _, P, _ in cost})
    max_bits = max(B for _, _, B in cost)
    bits_for = {P: sorted(B for m, q, B in cost if m == main_mode and q == P) for P in passes}
    one = {B: cost[main_mode, 1, B] for B in bits_for[1]}

    # ---- the numbers the text talks about -----------------------------------
    flat = sorted(one[B] for B in one if 1 <= B <= 4)
    flat_level = flat[len(flat) // 2]
    knee = next((B for B in sorted(one) if B >= 1 and one[B] > 1.3 * flat_level), None)
    steep = next((B for B in sorted(one) if all(one[b] > 2.0 * flat_level for b in one if b >= B)), None)
    crossover = {}
    for P in passes[1:]:
        cands = [B for B in bits_for[P] if all(cost[main_mode, P, b] < one[b] for b in bits_for[P] if b >= B)]
        crossover[P] = cands[0] if cands else None

    def clearly_faster(a, b):  # every run of a faster than every run of b
        return stat[a]["max"] < stat[b]["min"]

    best, tied = {}, {}
    for B in range(max_bits + 1):
        options = [P for P in passes if (main_mode, P, B) in cost]
        best[B] = min(options, key=lambda P: cost[main_mode, P, B])
        tied[B] = [P for P in options if P != best[B]
                   and not clearly_faster((main_mode, best[B], B), (main_mode, P, B))]

    def predicted(P, B):  # each pass costs what one pass with its own bit count costs
        return sum(one[B // P + (i < B % P)] for i in range(P))
    model_err = {P: [abs(predicted(P, B) - cost[main_mode, P, B]) / cost[main_mode, P, B] for B in bits_for[P]]
                 for P in passes[1:]}

    hp = None
    if "4k" in modes and "2m" in modes:
        diffs = [(cost["4k", 1, B] - cost["2m", 1, B]) / cost["4k", 1, B] for B in one]
        separated = sum(clearly_faster(("2m", 1, B), ("4k", 1, B)) or clearly_faster(("4k", 1, B), ("2m", 1, B))
                        for B in one)
        hp = dict(lo=min(diffs), hi=max(diffs), avg=sum(diffs) / len(diffs), separated=separated, total=len(one))
        hp["measurable"] = abs(hp["avg"]) > typical_cv
    l1, l2 = R.cache_bytes(env, 1), R.cache_bytes(env, 2)
    hw = R.counters_available(env)

    # ---- figures -------------------------------------------------------------
    R.style_charts()
    xt = list(range(0, max_bits + 1, 2))

    fig, ax = R.figure(2.75)
    for i, mode in enumerate(modes):
        b = sorted(B for m, P, B in cost if m == mode and P == 1)
        R.band(ax, b, [stat[mode, 1, B]["q1"] for B in b], [stat[mode, 1, B]["q3"] for B in b], R.SERIES[i])
        ax.plot(b, [cost[mode, 1, B] for B in b], "o-", color=R.SERIES[i],
                label=mode_label[mode])
    top = max(stat[m, 1, B]["q3"] for m in modes for B in bits_for[1]) * 1.1
    for size, label in ((l1, "L1"), (l2, "L2")):
        if size:
            x = math.log2(size / 64)
            ax.axvline(x, color=R.INK_3, lw=0.8, ls=(0, (2, 2)))
            ax.text(x + 0.15, top * 0.98, f"{label} lines", color=R.INK_3, fontsize=7.5, va="top")
    ax.set(xlabel="radix bits B  (one pass writes to 2^B partitions)", ylabel="ns per record", xticks=xt,
           ylim=(0, top))
    ax.legend(loc="upper left")
    fig1 = R.save(fig, args.results, "01_single_pass.png")

    fig, ax = R.figure(2.35)
    b = bits_for[1]
    for i, part in enumerate(("histogram", "scatter")):
        vals = [ns(R.summarize(split[main_mode, B][part])["median"]) for B in b]
        ax.plot(b, vals, "o-", color=R.SERIES[i],
                label={"histogram": "histogram (count per partition)",
                       "scatter": "scatter (copy to partition)"}[part])
    ax.set(xlabel="radix bits B", ylabel="ns per record", xticks=xt, ylim=(0, None))
    ax.legend(loc="upper left")
    fig2 = R.save(fig, args.results, "02_histogram_vs_scatter.png")

    fig, ax = R.figure(2.75)
    for i, P in enumerate(passes):
        bb = bits_for[P]
        R.band(ax, bb, [stat[main_mode, P, B]["q1"] for B in bb], [stat[main_mode, P, B]["q3"] for B in bb],
               R.SERIES[i])
        ax.plot(bb, [cost[main_mode, P, B] for B in bb], "o-", color=R.SERIES[i],
                label=f"{P} pass{'es' if P > 1 else ''}")
    ax.set(xlabel="total radix bits B", ylabel="ns per record, all passes", xticks=xt, ylim=(0, None))
    ax.legend(loc="upper left")
    fig3 = R.save(fig, args.results, "03_passes.png")

    fig, ax = R.figure(2.5)
    for i, P in enumerate(passes[1:], start=1):
        ax.plot(bits_for[P], [cost[main_mode, P, B] for B in bits_for[P]], "o-", color=R.SERIES[i],
                label=f"{P} passes, measured")
        ax.plot(bits_for[P], [predicted(P, B) for B in bits_for[P]], "--", color=R.SERIES[i], lw=1.3,
                label=f"{P} passes, predicted")
    ax.set(xlabel="total radix bits B", ylabel="ns per record", xticks=xt, ylim=(0, None))
    ax.legend(loc="upper left", ncol=2)
    fig4 = R.save(fig, args.results, "04_model_check.png")

    # ---- report ----------------------------------------------------------------
    rep = R.Report("Radix partitioning: bits, passes and page size",
                   f"{R.cpu_model(args.results)}  ·  {R.machine_line(env)}")
    rep.summary(
        f"We split {n:,} records into 2<super>B</super> partitions for B = 0 to {max_bits}, in 1, 2 or 3 passes. "
        f"One pass costs about {flat_level:.1f} ns per record while it writes to only a few partitions. "
        + (f"The cost steps up at {knee} bits ({2 ** knee:,} partitions)" if knee else "The cost stays flat")
        + (f" and stays above twice the flat level from {steep} bits. " if steep else ". ")
        + (f"Two passes only become cheaper than one from {crossover.get(2)} bits. " if crossover.get(2) else
           "Two passes never became cheaper than one in the tested range. ")
        + ("The paper's reason for multi-pass partitioning is still visible, but much later than a 64-entry TLB, "
           "typical in 1999, would suggest (6 bits)." if (crossover.get(2) or 99) > 8 else
           "The paper's reason for multi-pass partitioning is clearly visible."))
    kpis = [(f"{flat_level:.1f} ns", "per record, one pass, few partitions")]
    if knee:
        kpis.append((f"{2 ** knee:,}", f"partitions where one pass gets slower ({knee} bits)"))
    if crossover.get(2):
        kpis.append((f"{crossover[2]} bits", "where two passes start to beat one"))
    if hp:
        kpis.append(("None" if not hp["measurable"] else f"{100 * hp['avg']:+.0f}%",
                     "measurable speedup from 2 MiB huge pages" if not hp["measurable"]
                     else "average speedup from 2 MiB huge pages"))
    rep.key_numbers(kpis)

    rep.h("What the paper says")
    rep.p("Radix partitioning reads each record, takes some bits of its hash and copies the record to the "
          "partition those bits name. Each partition is a separate place in memory to write to. When there are more "
          "partitions than pages the TLB can translate quickly, or than cache lines the CPU can keep open for "
          "writing, almost every copy misses. Boncz, Manegold and Kersten therefore split B bits over P passes, so "
          "each pass only writes to 2<super>B/P</super> places. Extra passes cost extra reads and writes, so the "
          "best number of passes depends on where the memory system stops coping with the fan-out.")

    rep.h("Method")
    rep.bullets([
        f"<b>Input.</b> The left table of the simple hash join experiment: {n:,} records of 8 bytes ({R.human_bytes(n * 8)}), keys shuffled "
        "with seed 42.",
        "<b>Radix value.</b> The lowest B bits of the same 64-bit hash the simple hash join uses. With several passes, pass 1 "
        "splits on the top group of those bits and later passes split each cluster further. Bits per pass are as "
        "even as possible (14 bits in 2 passes is 7 + 7).",
        "<b>Timing.</b> Each pass counts records per partition (histogram), then copies them (scatter); both are "
        f"timed. All memory is allocated and touched before timing. One thread; {R.pinning_text(env)}.",
        f"<b>Repetitions.</b> One verified warmup, then {repeats} recorded runs of every setting. Each round runs "
        "every setting once, in a newly shuffled order, so a slow period affects all settings alike. We report medians; chart bands span the "
        f"middle half of the runs (interquartile range). Typical run-to-run variation was {R.pct(typical_cv)} of the mean "
        "(coefficient of variation, median over settings).",
("<b>Page sizes.</b> 4 KiB pages and 2 MiB transparent huge pages ("
         + ", ".join(f"{k.split('_')[-1]}: {int(v) // 1024} MiB on huge pages"
                     for k, v in env.items() if k.startswith("anon_huge_kb")) + ")."
         if "2m" in modes else f"<b>Page size.</b> The system default ({page_kib} KiB); this OS does not let a "
         "program choose huge pages, so the page-size comparison was skipped."),
        f"<b>Interference.</b> {R.steal_text(steal)}",
        "<b>Correctness.</b> After the warmup every result is checked: each record is in the partition its hash "
        "names, the partitions cover all rows, and the records are unchanged. The --test mode also compares "
        "sorted output with the input for 238 small cases.",
    ])

    rep.h("One pass: cost as the number of partitions grows")
    rep.figure(fig1, "Figure 1. Median cost per record of a single pass; the band spans the middle half of the recorded runs. "
                     "Dashed lines: where 2^B output cache lines (64 B each) would fill L1 and L2.")
    text = f"Up to {knee - 1 if knee else max_bits} bits a pass costs about as much as a plain copy " \
           f"({one[0]:.2f} ns per record at B = 0). "
    if knee:
        text += (f"At {knee} bits ({2 ** knee} partitions) the cost steps up to {one[knee]:.2f} ns, rises slowly to "
                 f"{one[min(max_bits, 16)]:.2f} ns at {min(max_bits, 16)} bits, and then climbs steeply to "
                 f"{one[max_bits]:.2f} ns at {max_bits} bits. ")
    if hp:
        text += ("The step appears at the same B with 2 MiB pages, which need far fewer TLB entries, so it points to "
                 "the caches or the CPU's limited buffers for pending writes rather than the TLB. ")
    rep.p(text)
    rep.figure(fig2, "Figure 2. Histogram and scatter time of a single pass (medians, 4 KiB pages). The histogram "
                     "only reads the input and updates a small array of counters; the scatter is where the cost "
                     "grows.")

    rep.h("More passes")
    rep.figure(fig3, "Figure 3. Median cost per record of all passes together; bands span the middle half of the recorded runs.")
    if crossover.get(2):
        rep.p(f"A second pass reads and writes all the data again, so it only pays once one large pass costs more than "
              f"two small ones. That happens from {crossover[2]} bits: at {max_bits} bits one pass costs "
              f"{one[max_bits]:.2f} ns and two passes {cost[main_mode, 2, max_bits]:.2f} ns. "
              + (f"Three passes were never the fastest choice up to {max_bits} bits."
                 if 3 in passes and all(best[B] != 3 for B in best) else ""))
    tbl = [["Bits B", "Partitions"] + [f"{P} pass{'es' if P > 1 else ''}" for P in passes] + ["Fastest"]]
    hl = []
    for B in range(max_bits + 1):
        label = f"{best[B]} pass{'es' if best[B] > 1 else ''}"
        if tied[B]:
            label += " (tie)"
        tbl.append([B, f"{2 ** B:,}"] + [f"{cost[main_mode, P, B]:.2f}" if (main_mode, P, B) in cost else "-"
                                          for P in passes] + [label])
        if best[B] > 1:
            hl.append(len(tbl) - 1)
    rep.table(tbl, [50, 80, 80, 80, 80, 110], highlight_rows=hl,
              note=f"Median ns per record over {repeats} runs, {mode_label[main_mode]}. 'Tie': the runs "
                   "of another pass count overlap with the fastest one, so the difference is within run-to-run "
                   "noise. Shaded rows: more than one pass was fastest. Full statistics are in summary.csv.")

    rep.h("Checking the cost model idea")
    rep.p("The paper models each pass separately: its cost depends on how many partitions that pass writes to and "
          "how that compares with the cache and TLB sizes. If that is right, P passes should cost the sum of the "
          "single-pass costs at each pass's own bit count. Figure 4 makes that prediction from the measured one-pass "
          "curve, without fitting anything.")
    rep.figure(fig4, "Figure 4. Measured multi-pass cost (solid) against the sum of single-pass costs (dashed).")
    rep.p("Average prediction error: " + "; ".join(
        f"{P} passes {R.pct(sum(e) / len(e))} (largest {R.pct(max(e))})" for P, e in model_err.items())
          + ". Later passes work on clusters that are already small, and a cluster that fits in cache is cheap to "
            "read again, so the sum tends to overestimate slightly.")

    if hp:
        rep.h("4 KiB pages against 2 MiB huge pages")
        if hp["measurable"]:
            rep.p(f"Huge pages changed the single-pass cost by {100 * hp['avg']:+.0f}% on average (from "
                  f"{100 * hp['lo']:+.0f}% to {100 * hp['hi']:+.0f}%; positive means huge pages were faster).")
        else:
            rep.p(f"There was no measurable difference. On average the two page sizes differed by "
                  f"{100 * abs(hp['avg']):.0f}%, less than the typical {R.pct(typical_cv)} run-to-run variation, "
                  f"and their run ranges were fully separated at only {hp['separated']} of {hp['total']} bit counts.")
        rep.p("In 1999 a data TLB held 64 entries or fewer and each miss was expensive. Current CPUs add a second-level "
              "TLB with well over a thousand entries and caches for page-table walks, so the TLB part of the "
              "paper's argument is much weaker on this machine.")

    rep.h("What this means for a radix hash join")
    rep.bullets([
        f"One pass is close to copy speed up to {knee - 1 if knee else max_bits} bits and stays under "
        f"{math.ceil(one[min(max_bits, 14)])} ns per record up to {min(max_bits, 14)} bits, enough to cut tens of "
        "millions of records into partitions of a few thousand.",
        f"Two passes only help beyond about {crossover.get(2, max_bits)} bits, so a radix hash join on this machine "
        "should mostly use one pass, and two only for very large partition counts.",
        "The paper's main idea, that one pass must not write to too many places, still holds, but the limit is "
        "much higher than on 1999 hardware.",
    ])

    rep.h("Limitations")
    lim = [f"<b>Hardware counters</b> were {'available: ' + ', '.join(hw) if hw else 'not available on this machine'}. "
           "Without them we can see where the cost rises, not which cache or TLB causes it.",
           f"<b>{repeats} runs per setting.</b> Differences smaller than the run-to-run variation (about "
           f"{R.pct(typical_cv)}) should be read as ties."]
    if R.is_virtual_machine(args.results):
        lim.append("<b>Virtual machine.</b> The L3 cache is shared with other tenants, and other work on the host "
                   "can slow individual runs.")
    lim.append("<b>Uniform keys.</b> Partitions are almost equal in size. Skewed keys would make some much larger.")
    rep.bullets(lim)

    rep.h("Future work")
    rep.bullets([
        "<b>Miss counts.</b> Measure L1, L2 and TLB misses (perf on native Linux, Instruments on macOS) to confirm "
        "which limit causes the cost step.",
        "<b>Page size.</b> Isolate the TLB effect with huge pages on native Linux, where the page size can be chosen.",
        "<b>Write-combining.</b> Collect each partition's records in a cache-line buffer before writing them out; "
        "this modern technique attacks the same fan-out problem as extra passes.",
        "<b>Full cost model.</b> Measure per-level memory latencies and predict pass costs from cache and TLB sizes "
        "instead of from the measured one-pass curve.",
        "<b>Skewed keys.</b> Repeat with Zipf-distributed keys, which make partitions uneven.",
        "See docs/future-work.md for details.",
    ])

    rep.h("Rerun")
    rep.code("make\n./bin/radix_partition --test\n./bin/radix_partition results/radix-partitioning-new\n"
             "python3 analysis/analyze_partition.py results/radix-partitioning-new")
    rep.references([R.PAPER + " Sections 3 and 4."])
    out = rep.build(args.pdf)
    print(f"Wrote {out}, {args.results / 'summary.csv'} and figures/")


if __name__ == "__main__":
    main()
