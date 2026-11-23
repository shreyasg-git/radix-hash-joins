"""Experiment 5 analysis: per-phase times and speedup with 1, 2 and 4 threads.

Usage: python3 analysis/analyze_parallel.py results/05-parallel-join [--pdf reports/05-parallel-join.pdf]
Writes summary.csv and figures/ into the results folder and the PDF report.
"""
import argparse
from collections import defaultdict
from pathlib import Path

import numpy as np

import report_lib as R

PHASES = {"simple": ["build", "probe"], "radix": ["partition_r", "partition_s", "build", "probe"]}
LABEL = {"partition_r": "partition R", "partition_s": "partition S", "build": "build", "probe": "probe",
         "total": "total"}
COLOR = {"partition_r": R.SERIES[2], "partition_s": "#8fd8bc", "build": R.SERIES[0], "probe": R.SERIES[1],
         "total": R.INK}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, nargs="?", default=R.ROOT / "results" / "05-parallel-join")
    parser.add_argument("--pdf", type=Path, default=R.REPORTS / "05-parallel-join.pdf")
    args = parser.parse_args()
    env = R.read_environment(args.results)
    rows = R.read_csv(args.results / "timings.csv")
    repeats = int(env["repeats"])
    n = int(env["rows"])
    cpus = int(env.get("online_cpus", "0"))

    values = defaultdict(list)
    for r in rows:
        values[r["algorithm"], int(r["threads"]), r["phase"]].append(float(r["ms"]))
    assert all(len(v) == repeats for v in values.values()), "missing runs"
    threads = sorted({k[1] for k in values})
    st = {k: R.summarize(v) for k, v in values.items()}
    med = {k: s["median"] for k, s in st.items()}
    speed = {k: med[k[0], threads[0], k[2]] / v for k, v in med.items()}
    ci = {k: R.bootstrap_ratio_ci(values[k[0], threads[0], k[2]], v) for k, v in values.items()}
    steal = R.steal_summary([r for r in rows if r["phase"] == "total"], "ms")
    summary = [{"algorithm": a, "threads": t, "phase": ph, "runs": s["n"], "median_ms": round(s["median"], 3),
                "mean_ms": round(s["mean"], 3), "sd_ms": round(s["sd"], 3), "cv_percent": round(100 * s["cv"], 2),
                "min_ms": round(s["min"], 3), "max_ms": round(s["max"], 3),
                "speedup_vs_1_thread": round(speed[a, t, ph], 3)} for (a, t, ph), s in sorted(st.items())]
    R.write_summary(args.results / "summary.csv", summary)
    typical_cv = sorted(s["cv"] for s in st.values())[len(st) // 2]
    tmax = max(threads)
    tcpu = max([t for t in threads if not cpus or t <= cpus] or threads)
    # M5 copy: compare with the one-thread programs of experiment 4, and say how the radix setting was chosen.
    exp4 = R.ROOT / "results" / "04-radix-join" / "summary.csv"
    exp4_rows = [r for r in R.read_csv(exp4) if int(r["rows_per_input"]) == n] if exp4.exists() else []
    exp4_simple = next((r for r in exp4_rows if r["algorithm"] == "simple"), None)
    exp4_radix = [r for r in exp4_rows if r["algorithm"] == "radix"]
    exp4_best = min(exp4_radix, key=lambda r: float(r["total_median_ms"])) if exp4_radix else None
    if exp4_best and (exp4_best["total_bits"], exp4_best["passes"]) == (env.get("radix_bits"), env.get("radix_passes")):
        setting_source = "the best single-thread setting at this size in experiment 4"
    elif exp4_best:
        setting_source = (f"chosen by hand; the best single-thread setting at this size in experiment 4 was "
                          f"{exp4_best['total_bits']} bits in {exp4_best['passes']} pass"
                          f"{'es' if exp4_best['passes'] != '1' else ''}")
    else:
        setting_source = "chosen by hand; experiment 4 has no run at this size"
    cas = env.get("simple_insert", "").startswith("cas")
    insert_text = ("A bucket head is updated with an atomic compare-and-swap in release order (the v2 way): before "
                   "each insert the core first finishes every earlier memory write, so inserts cannot overlap."
                   if cas else
                   "A bucket head is updated with one atomic swap: the new row becomes the head and the old head "
                   "becomes its next link, so no insert is lost. No ordering is requested per insert, because no "
                   "thread reads the table before every thread has passed the barrier after the build.")
    if exp4_simple:
        insert_text += (f" The one-thread run uses the same code: it built the table in {med['simple', threads[0], 'build']:.0f} ms, "
                        f"against {float(exp4_simple['build_median_ms']):.0f} ms for the plain one-thread build of "
                        "experiment 4.")
    else:
        insert_text += " The one-thread run uses the same code."

    # ---- figures ---------------------------------------------------------------
    R.style_charts()
    fig, ax = R.figure(2.8)
    groups = [(a, t) for a in ("simple", "radix") for t in threads]
    x = np.arange(len(groups)) + np.array([0 if g[0] == "simple" else 0.7 for g in groups])
    seen = set()
    for i, (a, t) in enumerate(groups):
        bottom = 0
        for ph in PHASES[a]:
            v = st[a, t, ph]["mean"]
            ax.bar(x[i], v, 0.66, bottom=bottom, color=COLOR[ph], edgecolor="white", linewidth=0.8,
                   label=LABEL[ph] if ph not in seen else None)
            seen.add(ph)
            bottom += v
        ax.text(x[i], bottom + 15, f"{bottom:.0f}", ha="center", va="bottom", fontsize=7.5, color=R.INK_2)
    ax.set_xticks(x, [f"{a}\n{t} thread{'s' if t > 1 else ''}" for a, t in groups])
    ax.set(ylabel="mean time (ms)")
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper right")
    fig1 = R.save(fig, args.results, "01_phase_times.png")

    fig, axes = R.figure(2.6, ncols=2, sharey=True)
    for ax, a in zip(axes, ("simple", "radix")):
        ax.plot(threads, threads, color=R.INK_3, lw=1, ls=(0, (3, 2)), label="perfect scaling")
        for ph in PHASES[a] + ["total"]:
            ax.plot(threads, [speed[a, t, ph] for t in threads], "o-", color=COLOR[ph], label=LABEL[ph],
                    lw=2.2 if ph == "total" else 1.5)
        if cpus and cpus < tmax:
            ax.axvspan(cpus, tmax, color="#f4f3f0", zorder=0)
            ax.text(cpus + 0.08, tmax * 0.97, "more threads\nthan CPUs", fontsize=7, color=R.INK_3, va="top")
        ax.set(title=f"{a} hash join", xlabel="threads", xticks=threads)
        ax.legend(loc="upper left", fontsize=7.2)
    axes[0].set_ylabel("speedup over 1 thread")
    fig2 = R.save(fig, args.results, "02_speedup.png")

    # ---- report ------------------------------------------------------------------
    rep = R.Report("Parallel simple and radix hash joins",
                   f"Experiment 5  ·  {R.cpu_model(args.results)}  ·  {n:,} rows per input  ·  {R.machine_line(env)}")
    rep.summary(
        f"With {tcpu} thread{'s' if tcpu > 1 else ''} the radix join ran {speed['radix', tcpu, 'total']:.2f} times as "
        f"fast as with one, and the simple join {speed['simple', tcpu, 'total']:.2f} times. The radix join stayed faster "
        f"at every thread count: {med['radix', tcpu, 'total']:.0f} ms against {med['simple', tcpu, 'total']:.0f} ms "
        f"with {tcpu} threads. "
        + (f"This machine has only {cpus} CPUs, so the {tmax}-thread runs share CPUs and show oversubscription rather "
           f"than a {tmax}-way speedup. Rerun on a machine with at least {tmax} cores for the full extension."
           if cpus and cpus < tmax else ""))
    rep.key_numbers([
        (f"{speed['radix', tcpu, 'total']:.2f}x", f"radix join speedup, {tcpu} threads"),
        (f"{speed['simple', tcpu, 'total']:.2f}x", f"simple join speedup, {tcpu} threads"),
        (f"{med['simple', tcpu, 'total'] / med['radix', tcpu, 'total']:.1f}x",
         f"radix over simple, {tcpu} threads"),
        (f"{cpus}", "CPUs on the test machine"),
    ])

    rep.h("How the joins were parallelized")
    rep.bullets([
        "<b>Simple join, build.</b> Each thread clears its share of the bucket array, then inserts its share of R into "
        "the one shared table. " + insert_text,
        "<b>Simple join, probe.</b> Each thread probes the shared table with its share of S. No writes, no locks.",
        "<b>Radix join, partition.</b> In the first pass each thread counts its share of the input per partition. The "
        "counts are combined so every thread knows exactly where to write, then all threads copy their records at once "
        "without locks. In later passes whole clusters are handed out through a shared atomic counter.",
        "<b>Radix join, build and probe.</b> Partitions are handed out in batches of 16 through the shared counter. All "
        "tables are built first, then all are probed, so the two phases can be timed separately.",
    ])

    rep.h("Method")
    rep.bullets([
        "<b>Phase times.</b> Every phase ends at a barrier, and its time is the wall-clock time until the slowest "
        "thread finishes.",
        f"<b>Settings.</b> {n:,} rows per input, {env.get('radix_bits')} radix bits in {env.get('radix_passes')} "
        f"pass{'es' if env.get('radix_passes') != '1' else ''} ({setting_source}). " + ("Thread i is pinned to the i-th CPU." if not env.get("thread_pinning", "").startswith("not")
                              else R.pinning_text(env) + "."),  # M5 copy: "macOS" keeps its lower-case m
        f"<b>Repetitions.</b> One verified warmup, then {repeats} recorded runs of every thread count and join. Each "
        "round runs every combination once in a newly shuffled order. Medians in text and tables, means in the "
        f"stacked bars, 95% bootstrap confidence intervals for speedups. Typical run-to-run variation: "
        f"{R.pct(typical_cv)} of the mean.",
        f"<b>Interference.</b> {R.steal_text(steal)}",
        "<b>Correctness.</b> Every run is checked against the expected match count and checksum. The --test mode "
        "compares 1 to 4 threads with a nested-loop join, and make check also runs it under ThreadSanitizer.",
    ])

    rep.h("Per-phase times")
    rep.figure(fig1, "Figure 1. Mean phase times; the label is the mean total in ms. Each phase ends when the slowest "
                     "thread reaches the barrier.")
    tbl = [["Join", "Phase"] + [f"{t} thr. (ms)" for t in threads] + [f"Speedup {t} thr. (95% CI)" for t in threads[1:]]]
    hl = []
    for a in ("simple", "radix"):
        for ph in PHASES[a] + ["total"]:
            tbl.append([a if ph == PHASES[a][0] else "", LABEL[ph]] + [R.fmt(med[a, t, ph], 1) for t in threads]
                       + [f"{speed[a, t, ph]:.2f}x ({ci[a, t, ph][0]:.2f}–{ci[a, t, ph][1]:.2f})" for t in threads[1:]])
            if ph == "total":
                hl.append(len(tbl) - 1)
    rep.table(tbl, [44, 64] + [52] * len(threads) + [92] * (len(threads) - 1), highlight_rows=hl,
              align="ll" + "r" * (len(threads) * 2 - 1),
              note=f"Medians of {repeats} runs. Speedup = 1-thread median / median with that many threads.")

    rep.h("Speedup")
    rep.figure(fig2, "Figure 2. Speedup of each phase over one thread. The dashed line is perfect linear scaling; "
                     "the shaded area has more threads than the machine has CPUs.")
    at = {f"{a} {LABEL[ph]}": speed[a, tcpu, ph] for a in ("simple", "radix") for ph in PHASES[a]}
    fastest, slowest = max(at, key=at.get), min(at, key=at.get)
    rep.p(f"With {tcpu} threads the best-scaling phase was the {fastest} ({at[fastest]:.2f}x) and the weakest the "
          f"{slowest} ({at[slowest]:.2f}x). Partitioning streams the whole input from memory and back, so it is "
          "limited by memory bandwidth and by combining the per-thread counts. Radix build and probe work on "
          "cache-resident tables and are mostly computation. The simple join's probes are random memory accesses; "
          "more threads keep more of them in flight, so it also scales well until the memory system is saturated. "
          "Because both joins scale similarly here, the radix join keeps its single-thread advantage.")
    if exp4_rows:
        same = [r for r in exp4_radix if r["total_bits"] == env.get("radix_bits") and r["passes"] == env.get("radix_passes")]
        if same:
            rep.p(f"For the same input and setting, the one-thread radix join took {med['radix', 1, 'total']:.0f} ms here "
                  f"against {float(same[0]['total_median_ms']):.0f} ms in experiment 4. The difference is mainly the "
                  "cost of building every table before probing any, which this program does so that build and probe "
                  "can be timed separately.")

    rep.h("Limitations")
    rep.bullets([
        (f"<b>{cpus} CPUs.</b> Runs with more threads than CPUs are time-shared, so they show oversubscription, not "
         "parallel speedup." if cpus and cpus < tmax else f"<b>{cpus} CPUs</b> were available."),
        "<b>Shared host.</b> Virtual CPUs may be hyperthreads of one physical core, and the host runs other work. "
        "Scaling on a dedicated laptop will differ." if R.is_virtual_machine(args.results) else
        "<b>Background work.</b> Other programs running on the machine can affect individual runs.",
        "<b>One radix setting.</b> The best number of bits can change with threads, because each core has its own L2.",
    ])
    rep.h("Rerun")
    rep.code("make\n./bin/parallel_join --test\n./bin/parallel_join results/05-parallel-join-new --threads 1,2,4\n"
             "python3 analysis/analyze_parallel.py results/05-parallel-join-new")
    out = rep.build(args.pdf)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
