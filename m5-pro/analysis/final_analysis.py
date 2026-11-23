"""Final analysis: which conclusions of the paper are still visible?

Reads the summary.csv files written by the other analysis scripts, so run
those first (or run scripts/run_all.sh). Writes reports/07-final-analysis.pdf
(M5 copy: 07, because experiment 6 is the bucket and hash sweep).

Usage: python3 analysis/final_analysis.py
"""
import argparse
from collections import defaultdict
from pathlib import Path

import report_lib as R

import os
RES = Path(os.environ.get("M5_RESULTS", R.ROOT / "results")).resolve()  # M5 copy: results folder can be chosen


TAG = ""  # set from --tag: experiments 3-5 are read from results/NN-name-TAG


def folder_of(name):
    return RES / (name + TAG if name[:2] in ("03", "04", "05") else name)


def load(name):
    path = folder_of(name) / "summary.csv"
    return R.read_csv(path) if path.exists() else None


def cpu_of(name):
    if (folder_of(name) / "environment.txt").exists():
        model = R.cpu_model(folder_of(name))
        if model != "unknown CPU":
            return model
    path = folder_of(name) / "system.txt"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("Model name:") or line.startswith("machdep.cpu.brand_string:"):
                return line.split(":", 1)[1].strip()
    return "not recorded"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, default=None)
    parser.add_argument("--tag", default="", help="read results/03-...-TAG etc. (as written by run_all.sh)")
    args = parser.parse_args()
    global TAG
    TAG = f"-{args.tag}" if args.tag else ""
    if args.pdf is None:
        args.pdf = R.REPORTS / f"07-final-analysis{TAG}.pdf"

    scan = load("01-stride-scan")
    simple = load("02-simple-hash-join")
    part = load("03-radix-partition")
    join = load("04-radix-join")
    par = load("05-parallel-join")
    env3 = R.read_environment(folder_of("03-radix-partition")) if part else {}
    env5 = R.read_environment(folder_of("05-parallel-join")) if par else {}
    vm = part is not None and R.is_virtual_machine(folder_of("03-radix-partition"))

    # ---- gather numbers --------------------------------------------------------
    facts = {}
    if scan:
        med = {(r["condition"], int(r["stride_bytes"])): float(r["median_ms"]) for r in scan}
        facts["scan_ratio"] = med["flushed", 256] / med["flushed", 1]
    if simple:
        s = {int(r["rows_per_input"]): float(r["total_median_ms"]) for r in simple}
        small, large = min(s), max(s)
        facts["w2"] = (small, s[small] * 1e6 / small, large, s[large] * 1e6 / large)
    if part:
        cost = {(r["page_mode"], int(r["passes"]), int(r["total_bits"])): float(r["median_ns_per_row"]) for r in part}
        main_mode = "4k" if any(m == "4k" for m, _, _ in cost) else "default"
        one = {B: v for (m, P, B), v in cost.items() if m == main_mode and P == 1}
        two = {B: v for (m, P, B), v in cost.items() if m == main_mode and P == 2}
        # M5 copy: same definitions as analyze_partition.py (flat = median of 2..8 bits; the knee is where the
        # cost STAYS more than 30% above it).
        flat_vals = sorted(one[B] for B in one if 2 <= B <= 8)
        flat = flat_vals[len(flat_vals) // 2]
        knee = next((B for B in sorted(one) if B >= 2 and all(one[b] > 1.3 * flat for b in one if b >= B)), max(one) + 1)
        steep = next((B for B in sorted(one) if all(one[b] > 2.0 * flat for b in one if b >= B)), max(one))
        cross = next((B for B in sorted(two) if all(two[b] < one[b] for b in two if b >= B)), None)
        hp = [(cost["4k", 1, B] - cost["2m", 1, B]) / cost["4k", 1, B] for B in one if ("2m", 1, B) in cost]
        cvs = sorted(float(r["cv_percent"]) / 100 for r in part if "cv_percent" in r)
        facts["part"] = dict(cv=cvs[len(cvs) // 2] if cvs else 0.0, flat=flat, knee=knee, steep=steep, cross=cross, top=max(one), one_top=one[max(one)],
                             two_top=two.get(max(one)), hp=sum(hp) / len(hp) if hp else None)
    if join:
        by = defaultdict(dict)
        for r in join:
            by[int(r["rows_per_input"])][(r["algorithm"], int(r["total_bits"]), int(r["passes"]))] = r
        sizes = sorted(by)
        rows = []
        for n in sizes:
            sim = float(by[n]["simple", 0, 0]["total_median_ms"])
            bk = min((k for k in by[n] if k[0] == "radix"), key=lambda k: float(by[n][k]["total_median_ms"]))
            rows.append((n, sim, float(by[n][bk]["total_median_ms"]), bk))
        facts["join"] = rows
        mc = folder_of("04-radix-join") / "model_choices.csv"
        facts["model"] = R.read_csv(mc) if mc.exists() else None
        worst2 = by[sizes[-1]]["radix", 2, 2]["total_median_ms"] if ("radix", 2, 2) in by[sizes[-1]] else None
        facts["join_worst"] = worst2
    if par:
        med = {(r["algorithm"], int(r["threads"]), r["phase"]): float(r["median_ms"]) for r in par}
        cpus = int(env5.get("online_cpus", "0"))
        threads = sorted({k[1] for k in med})
        pcores = cpus
        sysfile = folder_of("05-parallel-join") / "system.txt"
        if sysfile.exists():
            for line in sysfile.read_text().splitlines():
                if line.startswith("hw.perflevel0.physicalcpu:"):
                    pcores = int(line.split(":")[1])
        t = max([x for x in threads if x <= pcores] or threads)
        tmax = max(threads)
        facts["par"] = dict(t=t, cpus=cpus, tmax=tmax, pcores=pcores,
                            radix_max=med["radix", 1, "total"] / med["radix", tmax, "total"],
                            simple_max=med["simple", 1, "total"] / med["simple", tmax, "total"],
                            radix=med["radix", 1, "total"] / med["radix", t, "total"],
                            simple=med["simple", 1, "total"] / med["simple", t, "total"],
                            part=med["radix", 1, "partition_r"] / med["radix", t, "partition_r"],
                            probe=med["radix", 1, "probe"] / med["radix", t, "probe"],
                            sprobe=med["simple", 1, "probe"] / med["simple", t, "probe"],
                            rt=med["radix", t, "total"], st=med["simple", t, "total"])

    rep = R.Report("Which conclusions of the paper still hold?",
                   "Final analysis of experiments 1 to 5 (experiment 6 tests our own design choice, not the paper)")
    rep.summary("Boncz, Manegold and Kersten argued in 1999 that main-memory joins are limited by memory access, not by "
          "computation, and that a join should therefore be organized around the caches and the TLB: partition the "
          "inputs into cache-sized pieces with a radix partitioning that uses several passes when one pass would "
          "write to too many places, and pick the parameters with a cost model. This report collects the evidence "
          "from experiments 1 to 5 and marks each conclusion as still visible, visible but weaker, or changed.")

    if "join" in facts and "part" in facts:
        n, sim, rad, bk = facts["join"][-1]
        rep.key_numbers([(f"{facts['scan_ratio']:.0f}x" if "scan_ratio" in facts else "-", "slower scan at stride 256 vs 1"),
                         (f"{sim / rad:.1f}x", f"radix over simple join at {n / 1e6:g}M rows"),
                         (f"{facts['part']['cross']} bits" if facts["part"]["cross"] else "never",
                          "where two passes beat one"),
                         (("n/a", "huge pages not selectable on this OS") if facts["part"]["hp"] is None else
                          ("No" if abs(facts["part"]["hp"]) <= facts["part"]["cv"] else "Yes",
                           "measurable huge-page effect"))])

    rep.h("Machines")
    machines = [["Experiment", "Machine", "Results folder"]]
    names = [("1. Strided scan", "01-stride-scan"), ("2. Simple hash join", "02-simple-hash-join"),
             ("3. Radix partitioning", "03-radix-partition"), ("4. Simple vs radix join", "04-radix-join"),
             ("5. Parallel joins", "05-parallel-join")]
    if folder_of("06-bucket-sweep").exists():  # M5 copy: experiment 6
        names.append(("6. Bucket count and hash", "06-bucket-sweep"))
    for label, folder in names:
        cpu = cpu_of(folder)
        if folder == "01-stride-scan" and cpu == "not recorded":
            cpu = "AMD Ryzen 7 7840HS laptop (Week 1, Ubuntu run in the repo)"
        machines.append([label, cpu, f"results/{folder_of(folder).name}"])
    new_cpu, old_cpu = cpu_of("03-radix-partition"), cpu_of("02-simple-hash-join")
    rep.table(machines, [120, 250, 133], align="lll",
              note=("All experiments ran on the same CPU." if new_cpu == old_cpu else
                    f"Experiments 1 and 2 ran on the team's Ryzen laptop; experiments 3 to 5 on {new_cpu}. "
                    "Absolute times are therefore not comparable across the two groups; each conclusion below is "
                    "drawn within one machine."))

    if "join" in facts:
        bests = [bk for _, _, _, bk in facts["join"]]
        passes_used = sorted({bk[2] for bk in bests})
        best_passes = ("one pass" if passes_used == [1] else
                       ", ".join(f"{p} pass{'es' if p > 1 else ''}" for p in passes_used))
        best_bits_lo, best_bits_hi = min(bk[1] for bk in bests), max(bk[1] for bk in bests)

    # ---- the verdict table -------------------------------------------------------
    verdict = [["Conclusion of the paper", "Our evidence", "Verdict"]]
    if "scan_ratio" in facts:
        verdict.append(["Memory access, not computation, dominates when data does not fit in cache.",
                        f"Exp. 1: the same number of byte reads took {facts['scan_ratio']:.0f}x as long at stride 256 "
                        "as at stride 1.", "Still visible"])
    if "w2" in facts:
        a, at, b, bt = facts["w2"]
        verdict.append(["A hash join slows down per tuple once its table outgrows the caches.",
                        f"Exp. 2: {at:.0f} ns per row at {a:,} rows, {bt:.0f} ns at {b:,} rows, for the same work per "
                        "row.", "Still visible"])
    if "join" in facts:
        n, sim, rad, bk = facts["join"][-1]
        n0, sim0, rad0, _ = facts["join"][0]
        verdict.append(["Partitioning into cache-sized pieces makes the join faster, despite the extra passes.",
                        f"Exp. 4: {sim / rad:.1f}x faster at {n:,} rows ({bk[1]} bits); "
                        f"{sim0 / rad0:.2f}x at {n0:,} rows, where everything already fits in cache.",
                        "Still visible"])
    if "part" in facts:
        p = facts["part"]
        verdict.append(["One radix pass gets slow when it writes to too many partitions.",
                        f"Exp. 3: about {p['flat']:.1f} ns per record from 2 to {p['knee'] - 1} bits; from {p['knee']} "
                        f"bits it stays more than 30% above that, and above twice that from {p['steep']} bits "
                        f"({p['one_top']:.1f} ns at {p['top']} bits).",
                        "Still visible"])
        verdict.append(["Several passes beat one pass for large numbers of partitions.",
                        (f"Exp. 3: two passes win only from {p['cross']} bits ({2 ** p['cross']:,} partitions). "
                         + (f"In Exp. 4 the best setting used {best_passes} at every size." if "join" in facts else "")
                         if p["cross"] else
                         "Exp. 3: two passes never beat one in the tested range."),
                        "Visible but much weaker" if p["cross"] else "Not visible in the tested range"])
        if p["hp"] is not None:
            noise = p["cv"]
            effect = (f"no measurable difference ({100 * abs(p['hp']):.0f}% on average, below the "
                      f"{100 * noise:.0f}% run-to-run variation)" if abs(p["hp"]) <= noise else
                      f"a {100 * p['hp']:+.0f}% average change")
            verdict.append(["TLB misses are a main cost of partitioning.",
                            f"Exp. 3: 2 MiB huge pages made {effect}, and the cost step appears at the same "
                            "fan-out with both page sizes.", "Changed by modern hardware"])
    if facts.get("model"):
        loss = [float(r["model_choice_slower_percent"]) for r in facts["model"]]
        good = sum(int(r.get("model_choice_within_noise", 0)) for r in facts["model"])
        verdict.append(["A cost model built from cache parameters can choose good join parameters.",
                        f"Exp. 4: a simplified model (calibrated miss penalties plus measured pass costs) chose a "
                        f"setting within run-to-run noise of the fastest at {good} of {len(loss)} sizes; its worst "
                        f"choice was {max(loss):.0f}% slower.", "Visible, approximately"])
    if "par" in facts:
        q = facts["par"]
        verdict.append(["(Extension) Partitioned joins parallelize well.",
                        f"Exp. 5: with {q['t']} threads (one per performance core) radix total {q['radix']:.2f}x, "
                        f"simple total {q['simple']:.2f}x; the radix join stayed faster ({q['rt']:.0f} vs {q['st']:.0f} ms)."
                        + (f" With {q['tmax']} threads, {q['tmax'] - q['pcores']} of them on the slower efficiency "
                           f"cores: radix {q['radix_max']:.2f}x, simple {q['simple_max']:.2f}x."
                           if q['tmax'] > q['pcores'] else ""),
                        "Partly tested" if q["cpus"] < q["tmax"] else "Still visible"])
    tone = {"Still visible": "#0f7a3d", "Visible but much weaker": "#a65f00", "Visible, approximately": "#a65f00",
            "Changed by modern hardware": "#b42318", "Partly tested": "#55544f",
            "Not visible in the tested range": "#b42318"}
    for row in verdict[1:]:
        row[2] = f'<font name="Sans-Semibold" color="{tone.get(row[2], R.INK)}">{row[2]}</font>'
    rep.table(verdict, [150, 263, 90], align="lll",
              note="Green: still visible. Amber: visible but weaker or approximate. Red: changed by modern hardware.")

    rep.h("Conclusions that are still visible")
    rep.p("The core argument holds on current hardware. Experiments 1 and 2 show that time per operation depends on "
          "where the data lives, not on how much work is done. Experiment 4 shows the payoff the paper promised: "
          "partitioning both inputs costs a full extra read and write of the data, yet the radix join is faster as "
          "soon as the simple join's table no longer fits in cache. Its time per row stays almost constant while "
          "the simple join's keeps growing. Most of the gain is in the probe phase, which is exactly the "
          "random-access phase the paper targets.")
    rep.h("Conclusions weakened or changed by modern hardware")
    rep.bullets([
        "<b>TLB.</b> In 1999 a data TLB had 64 entries or fewer, and each miss was expensive. Current CPUs add a "
        "second-level TLB with over a thousand entries and caches for page-table walks"
        + (", and Linux can back memory with 2 MiB pages; our partitioning cost hardly changed with them."
           if facts.get("part", {}).get("hp") is not None else
           ". This OS does not let a program choose huge pages, so we could not isolate the TLB effect."),
        "<b>Number of passes.</b> A single pass now tolerates a much larger fan-out (bigger L1 and L2 caches, more "
        "misses in flight, store buffers and prefetchers). "
        + (f"In experiment 4 the best setting used {best_passes} at every size, with {best_bits_lo} to {best_bits_hi} "
           f"bits. " if "join" in facts else "")
        + (f"Multi-pass partitioning only paid off at {facts['part']['cross']} or more bits."
           if facts.get("part", {}).get("cross") else ""),
        "<b>Cache sizes.</b> L2 caches are now megabytes, so partitions can be much larger, and fewer radix bits "
        "are needed than the paper's L1-sized clusters suggest.",
        "<b>Out-of-order execution.</b> Current cores keep many cache misses in flight at once and prefetch "
        "aggressively. That softens the simple join's penalty, which limits the radix join's advantage"
        + (f" to {max(s / r for _, s, r, _ in facts['join']):.1f}x in our runs." if "join" in facts else "."),
    ])
    rep.h("Results affected by our experimental limitations")
    rep.bullets([
        "No hardware miss counts: experiments 1 and 2 did not collect them, and the machine used for experiments "
        "3 to 5 did not expose hardware counters to the programs. We infer cache effects from timings and data "
        "sizes, as the project allows.",
    ] + (["Experiments 3 to 5 ran on a virtual machine whose L3 cache is shared with other tenants, so the usable "
          "L3 is smaller and less predictable than its nominal size."] if vm else [])
      + ([f"With {facts['par']['cpus']} CPUs, runs with more threads share CPUs and do not show real scaling."]
         if facts.get("par") and facts["par"]["cpus"] < facts["par"]["tmax"] else [])
      + (["The experiments used two machines (see the Machines table), so absolute times are only compared within "
          "a machine."] if new_cpu != old_cpu else []) + [
        "Inputs have unique, uniformly hashed keys. Skewed or duplicate-heavy data would make partitions uneven.",
        "Matches are counted rather than materialized, which removes the output-writing cost from both joins.",
    ])
    rep.h("Future work")
    rep.p("What this project could not do, and how to do it next. docs/future-work.md has the details.")
    rep.bullets([
        "<b>Hardware miss counts.</b> WSL2, virtual machines and macOS do not let a program read the CPU's "
        "performance counters. Run experiments 3 and 4 on native Linux, where the programs already record L1, "
        "last-level cache and TLB misses, or use Instruments on macOS.",
    ] + ([  # M5 copy: only a gap when the experiments really ran on different machines
        "<b>One machine for everything.</b> Experiments 1 and 2 and experiments 3 to 5 ran on different machines. Port "
        "the Week 1 scan (it uses the x86 cache-flush instruction) and the Week 2 join (Linux CPU pinning), then rerun "
        "all five on one machine.",
    ] if new_cpu != old_cpu else []) + [
        "<b>Thread placement.</b> macOS cannot pin threads to cores or keep them off the slower efficiency cores. "
        "Repeat experiment 5 on Linux with pinned threads.",
        "<b>TLB and page size.</b> Huge pages cannot be chosen on macOS, so the TLB effect could only be tested on "
        "Linux. Repeat on native Linux with transparent and reserved huge pages, together with TLB miss counts.",
        "<b>Radix-join variant.</b> The paper also joins very small clusters with a nested loop instead of a hash "
        "table. We implemented only the partitioned hash join.",
        "<b>Full cost model.</b> Ours is fitted to the join measurements. Measure per-level memory latencies "
        "separately and implement the paper's miss-count formulas, including the TLB term, then predict without "
        "fitting.",
        "<b>Wider records.</b> The paper argues for narrow, decomposed storage. Vary the record width (16 to 64 bytes) "
        "to test it.",
        "<b>Data and output.</b> Skewed and duplicate keys, inputs of very different sizes, and writing the join "
        "result instead of counting it.",
        "<b>Modern partitioning.</b> Software write-combining buffers and non-temporal stores attack the same fan-out "
        "problem as multiple passes; compare them with the paper's approach.",
        "<b>Broader measurements.</b> More radix settings per thread count, more cores and NUMA machines, cold-cache "
        "runs, separate processes, CPU frequency and temperature logging, and more data seeds.",
    ])
    rep.references([R.PAPER])
    out = rep.build(args.pdf)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
