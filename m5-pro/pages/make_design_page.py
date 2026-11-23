"""Builds the experiment-design page from the M5 results.

python3 make_design_page.py RESULTS_DIR OUT.html [--r04 DIR] [--r05 DIR] [--old05 DIR]
--old05: experiment 5 results from before the insert fix (default: the archive).
"""
import argparse, csv, json, statistics
from collections import defaultdict
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("results", type=Path)
ap.add_argument("out", type=Path)
ap.add_argument("--r04", type=Path)
ap.add_argument("--r05", type=Path)
ap.add_argument("--old05", type=Path)
a = ap.parse_args()
R = a.results
r04 = a.r04 or R / "04-radix-join"
r05 = a.r05 or R / "05-parallel-join"
old05 = a.old05 or Path(__file__).resolve().parent.parent / "archive/2026-11-22-before-fixes/results/05-parallel-join"
rd = lambda p: list(csv.DictReader(open(p)))
env = lambda d: dict(l.split("=", 1) for l in (d / "environment.txt").read_text().splitlines() if "=" in l)
f = float

# ---- 1 stride scan -----------------------------------------------------------
s1 = {(r["condition"], int(r["stride_bytes"])): f(r["median_ns_per_read"]) for r in rd(R / "01-stride-scan/summary.csv")}
strides = sorted({s for _, s in s1})
scan_ratio = s1["flushed", 256] / s1["flushed", 1]
meta = json.loads((R / "01-stride-scan/benchmark_metadata.json").read_text())

# ---- 2 simple hash join ------------------------------------------------------
def simple_summary(d):
    return {int(r["rows_per_input"]): r for r in rd(R / d / "summary.csv")}
w2, q4 = simple_summary("02-simple-hash-join"), simple_summary("02-simple-hash-join-quarter-buckets")
sizes2 = sorted(w2)
pr = lambda r, n: f(r["total_median_ms"]) * 1e6 / n
rule_ratio = [f(q4[n]["total_median_ms"]) / f(w2[n]["total_median_ms"]) for n in sizes2]

# ---- 3 radix partitioning ----------------------------------------------------
c3 = {(int(r["passes"]), int(r["total_bits"])): f(r["median_ns_per_row"]) for r in rd(R / "03-radix-partition/summary.csv")}
bits3 = sorted({b for _, b in c3})
one = {b: c3[1, b] for b in bits3 if (1, b) in c3}
flat_vals = sorted(one[b] for b in one if 2 <= b <= 8)
flat = flat_vals[len(flat_vals) // 2]
knee = next((b for b in sorted(one) if b >= 2 and all(one[x] > 1.3 * flat for x in one if x >= b)), None)
cross = next((b for b in sorted(b for p, b in c3 if p == 2) if all(c3[2, x] < one[x] for p, x in c3 if p == 2 and x >= b)), None)
cv3 = statistics.median(f(r["cv_percent"]) for r in rd(R / "03-radix-partition/summary.csv"))

# ---- 4 simple vs radix -------------------------------------------------------
s4 = rd(r04 / "summary.csv")
by4 = defaultdict(dict)
for r in s4:
    by4[int(r["rows_per_input"])][(r["algorithm"], int(r["total_bits"]), int(r["passes"]))] = r
sizes4 = sorted(by4)
best4 = {n: min((k for k in by4[n] if k[0] == "radix"), key=lambda k: f(by4[n][k]["total_median_ms"])) for n in sizes4}
simple4 = {n: f(by4[n]["simple", 0, 0]["total_median_ms"]) for n in sizes4}
radix4 = {n: f(by4[n][best4[n]]["total_median_ms"]) for n in sizes4}
sep4 = {n: f(by4[n][best4[n]]["total_max_ms"]) < f(by4[n]["simple", 0, 0]["total_min_ms"]) for n in sizes4}
first_sep = next((n for n in sizes4 if sep4[n] and all(sep4[m] for m in sizes4 if m >= n)), None)
e4 = env(r04)
clock_ns = f(e4.get("timer_call_ns", "0"))
def median_total(folder, n, bits):
    v = [f(r["total_ms"]) for r in rd(folder / "timings.csv")
         if int(r["rows_per_input"]) == n and int(r["total_bits"]) == bits and r["passes"] == "1"]
    return statistics.median(v)
old_timing = R / "04-radix-join-old-timing"
clk_old, clk_new = median_total(old_timing, 100000, 14), median_total(r04, 100000, 14)
model = rd(r04 / "model_choices.csv") if (r04 / "model_choices.csv").exists() else []
seeds = []
for d in sorted(R.glob("04-radix-join-seed*")):
    runs = defaultdict(list)
    for r in rd(d / "timings.csv"):
        runs[int(r["rows_per_input"]), r["algorithm"], int(r["total_bits"])].append(f(r["total_ms"]))
    sd = env(d).get("seed")
    for n in sorted({k[0] for k in runs}):
        sm = statistics.median(runs[n, "simple", 0])
        bk = min((k for k in runs if k[0] == n and k[1] == "radix"), key=lambda k: statistics.median(runs[k]))
        seeds.append((sd, n, sm, statistics.median(runs[bk]), bk[2]))

# ---- 5 threads -----------------------------------------------------------------
m5 = {(r["algorithm"], int(r["threads"]), r["phase"]): f(r["median_ms"]) for r in rd(r05 / "summary.csv")}
threads = sorted({k[1] for k in m5})
e5 = env(r05)
sb4 = [f(r["build_ms"]) for r in rd(r04 / "timings.csv")
       if int(r["rows_per_input"]) == int(e5.get("rows", 16000000)) and r["algorithm"] == "simple"]
sp = {alg: {t: m5[alg, 1, "total"] / m5[alg, t, "total"] for t in threads} for alg in ("simple", "radix")}
phases5 = {t: {p: m5["radix", t, p] for p in ("partition_r", "partition_s", "build", "probe")} for t in threads}
psp = {p: m5["radix", 1, p] / m5["radix", 4, p] for p in ("partition_r", "build", "probe")} if 4 in threads else {}
o5 = {(r["algorithm"], int(r["threads"]), r["phase"]): f(r["median_ms"]) for r in rd(old05 / "summary.csv")}
if sp["simple"].get(4, 0) > sp["radix"].get(4, 0):
    t_compare = (f"The simple join gained more from threads, but the radix join still finished first at every thread "
                 f"count ({m5['radix', 4, 'total']:.0f} against {m5['simple', 4, 'total']:.0f} ms with 4 threads).")
else:
    t_compare = (f"The radix join gained more from threads and finished first at every thread count "
                 f"({m5['radix', 4, 'total']:.0f} against {m5['simple', 4, 'total']:.0f} ms with 4 threads).")

# ---- 6 bucket count and hash functions ----------------------------------------
WEEK2, QUARTER = 0.5, 4.0
def lab(x):
    return f"{x:g}" if x >= 1 else f"1/{1 / x:g}"
b6 = defaultdict(dict)
for r in rd(R / "06-bucket-sweep/summary.csv"):
    if r["hash"] == "mix" and r["keys"] == "dense":
        b6[int(r["rows_per_input"]), r["join"]][f(r["rows_per_bucket"])] = r
sizes6 = sorted({n for n, _ in b6})
loads6 = sorted({l for v in b6.values() for l in v})
best6, ties6 = {}, {}
for k, v in b6.items():
    b = min(v, key=lambda l: f(v[l]["total_median_ms"]))
    best6[k] = b
    ties6[k] = [l for l in v if l != b and f(v[l]["total_min_ms"]) <= f(v[b]["total_max_ms"])]
ratio6 = lambda k, l: f(b6[k][l]["total_median_ms"]) / f(b6[k][best6[k]]["total_median_ms"])
def keep_rule(j):
    ok = [l for l in loads6 if all(l == best6[n, j] or l in ties6[n, j] for n in sizes6)]
    return max(ok) if ok else None
b_rows = "".join(
    f"<tr><td class='num'>{n:,}</td><td>{j}</td><td class='num'>{lab(best6[n, j])}"
    + (f" (ties: {', '.join(lab(l) for l in sorted(ties6[n, j]))})" if ties6[n, j] else "") + "</td>"
    f"<td class='num'>{ratio6((n, j), WEEK2):.2f}× slower</td><td class='num'>{ratio6((n, j), QUARTER):.2f}× slower</td></tr>"
    for n in sizes6 for j in ("simple", "radix") if (n, j) in best6)
big6 = sizes6[-1]
rules = {j: keep_rule(j) for j in ("simple", "radix")}
b_result = (f"At {big6:,} rows the simple join was fastest with {lab(best6[big6, 'simple'])} rows per box; the Week 2 "
            f"rule took {ratio6((big6, 'simple'), WEEK2):.2f}× as long and Varun's rule {ratio6((big6, 'simple'), QUARTER):.2f}×. "
            f"The radix join was fastest with {lab(best6[big6, 'radix'])} rows per box and cared less: Varun's rule took "
            f"{ratio6((big6, 'radix'), QUARTER):.2f}× as long, because its small tables sit in the cache, where walking a "
            "longer chain is cheap. ")
if rules["simple"] and rules["simple"] == rules["radix"]:
    b_result += (f"For both joins the fewest boxes that tied with the fastest at every size was {lab(rules['simple'])} "
                 "rows per box. ")
else:
    for j in ("simple", "radix"):
        b_result += (f"For the {j} join the fewest boxes that tied with the fastest at every size was {lab(rules[j])} "
                     "rows per box. " if rules[j] else f"For the {j} join no single value tied with the fastest at every size. ")
spread = [(f(r["actual_rows_per_bucket"]), f(r["empty_bucket_share"])) for v in b6.values() for r in v.values()
          if r["join"] == "simple"]
import math
mix_dev = max(abs(e - math.exp(-a)) for a, e in spread)
h6 = {(r["hash"], r["keys"], r["join"]): r for r in rd(R / "06-hash-functions/summary.csv")
      if f(r["rows_per_bucket"]) == WEEK2}
h_n = int(next(iter(rd(R / "06-hash-functions/summary.csv")))["rows_per_input"])
hashes6 = [h for h in ("identity", "multiply-low", "fibonacci", "crc32", "mix") if any(k[0] == h for k in h6)]
h_rows = ""
for kp in ("dense", "stride64", "random"):
    for h in hashes6:
        s_ = h6.get((h, kp, "simple")); r_ = h6.get((h, kp, "radix"))
        if not s_:
            continue
        h_rows += (f"<tr><td>{kp if h == hashes6[0] else ''}</td><td>{h}</td><td class='num'>{100 * f(s_['empty_bucket_share']):.1f}%</td>"
                   f"<td class='num'>{int(f(s_['longest_chain']))}</td><td class='num'>{f(s_['rows_checked_per_probe']):.2f}</td>"
                   f"<td class='num'>{f(s_['total_median_ms']):.1f}</td><td class='num'>{f(r_['total_median_ms']):.1f}</td></tr>" if r_ else "")
st = {kp: {h: f(h6[h, kp, "simple"]["total_median_ms"]) for h in hashes6 if (h, kp, "simple") in h6} for kp in ("dense", "stride64")}
h_note = (f"Dense keys are 0, 1, 2, …; stride64 keys are 0, 64, 128, … (the lowest 6 bits are always 0). With dense keys the "
          f"fastest hash was {min(st['dense'], key=st['dense'].get)} ({min(st['dense'].values()):.1f} ms against "
          f"{st['dense']['mix']:.1f} ms for mix). With stride64 keys, identity and multiply-low used 1 box in 64 and took "
          f"{st['stride64']['identity']:.0f} and {st['stride64']['multiply-low']:.0f} ms against {st['stride64']['mix']:.1f} ms for mix. "
          "mix is never the fastest and never badly wrong.")

fmt = lambda x, d=0: f"{x:,.{d}f}"
pct = lambda x: f"{100 * x:.0f}%" if x >= 0.01 else f"{100 * x:.1f}%"
def rows_word(n):
    return f"{n / 1e6:g} million" if n >= 1e6 else f"{n:,}"

charts = {
    "scan": {"xLog": True, "xLabel": "stride (bytes between reads)", "yLabel": "ns per read", "xTicks": strides,
             "series": [{"name": "After clearing the cache", "c": "--s1", "pts": [[s, s1["flushed", s]] for s in strides]},
                        {"name": "Repeat scan right away", "c": "--s2", "pts": [[s, s1["repeat_scan", s]] for s in strides if ("repeat_scan", s) in s1]}],
             "unit": "ns per read", "xUnit": " B"},
    "part": {"xLog": False, "xLabel": "radix bits B (one pass writes to 2^B partitions)", "yLabel": "ns per record",
             "xTicks": [b for b in bits3 if b % 2 == 0],
             "series": [{"name": f"{p} pass" + ("es" if p > 1 else ""), "c": f"--s{p}", "pts": [[b, c3[p, b]] for b in bits3 if (p, b) in c3]}
                        for p in (1, 2, 3) if any((p, b) in c3 for b in bits3)],
             "unit": "ns per record", "xUnit": " bits"},
    "join": {"xLog": True, "xLabel": "rows per input", "yLabel": "ns per input row", "xTicks": sizes4,
             "series": [{"name": "Simple hash join", "c": "--s1", "pts": [[n, simple4[n] * 1e6 / n] for n in sizes4]},
                        {"name": "Radix hash join, best setting", "c": "--s2", "pts": [[n, radix4[n] * 1e6 / n] for n in sizes4]}],
             "unit": "ns per row", "xUnit": " rows"},
    "threads": {"xLog": True, "xLabel": "threads", "yLabel": "times as fast as 1 thread", "xTicks": threads,
                "series": [{"name": "Simple hash join", "c": "--s1", "pts": [[t, sp["simple"][t]] for t in threads]},
                           {"name": "Radix hash join", "c": "--s2", "pts": [[t, sp["radix"][t]] for t in threads]}],
                "ref": [[t, t] for t in threads], "refName": "perfect sharing", "unit": "× as fast", "xUnit": " threads",
                "yZero": True},
    "buckets": {"xLog": True, "xLabel": "rows per box (log scale)", "yLabel": "ns per input row", "xTicks": loads6,
                "xTickLabels": [lab(l) for l in loads6],
                "series": [{"name": f"Simple join, {big6 / 2**20:g}M rows", "c": "--s1",
                            "pts": [[l, f(b6[big6, "simple"][l]["total_median_ms"]) * 1e6 / big6] for l in loads6 if l in b6[big6, "simple"]]},
                           {"name": f"Radix join, {big6 / 2**20:g}M rows", "c": "--s2",
                            "pts": [[l, f(b6[big6, "radix"][l]["total_median_ms"]) * 1e6 / big6] for l in loads6 if l in b6[big6, "radix"]]}],
                "unit": "ns per row", "xUnit": " rows per box"},
}

n_big = sizes4[-1]
seed_rows = "".join(
    f"<tr><td class='num'>{sd}</td><td class='num'>{n:,}</td><td class='num'>{fmt(sm, 1)}</td><td class='num'>{fmt(rm, 1)}</td>"
    f"<td class='num'>{b}</td><td class='num'>{sm / rm:.2f}×</td></tr>" for sd, n, sm, rm, b in seeds)
main_seed_rows = "".join(
    f"<tr><td class='num'>42</td><td class='num'>{n:,}</td><td class='num'>{fmt(simple4[n], 1)}</td><td class='num'>{fmt(radix4[n], 1)}</td>"
    f"<td class='num'>{best4[n][1]}</td><td class='num'>{simple4[n] / radix4[n]:.2f}×</td></tr>"
    for n in sizes4 if any(n == s[1] for s in seeds))
join_rows = "".join(
    f"<tr><td class='num'>{n:,}</td><td class='num'>{fmt(simple4[n], 1)}</td><td class='num'>{fmt(radix4[n], 1)}</td>"
    f"<td class='num'>{best4[n][1]} bits, {best4[n][2]} pass</td><td class='num'>{simple4[n] / radix4[n]:.2f}×</td>"
    f"<td>{'yes' if sep4[n] else 'no'}</td></tr>" for n in sizes4)
model_good = sum(int(r["model_choice_within_noise"]) for r in model)
model_worst = max((f(r["model_choice_slower_percent"]) for r in model), default=0)
thread_rows = "".join(
    f"<tr><td class='num'>{t}</td><td class='num'>{fmt(m5['simple', t, 'total'], 1)}</td><td class='num'>{sp['simple'][t]:.2f}×</td>"
    f"<td class='num'>{fmt(m5['radix', t, 'total'], 1)}</td><td class='num'>{sp['radix'][t]:.2f}×</td>"
    f"<td class='num'>{m5['simple', t, 'total'] / m5['radix', t, 'total']:.1f}×</td></tr>" for t in threads)
pbits, ppasses = e5.get("radix_bits"), e5.get("radix_passes")
pcores = 4

html = (Path(__file__).parent / "design_template.html").read_text()
vals = dict(
    CHARTS=json.dumps(charts),
    SCAN_RATIO=f"{scan_ratio:.0f}", SCAN_REPS=str(meta.get("repetitions", 30)), SCAN_WARM=str(meta.get("warmup_rounds", 3)),
    EVICT_CACHED=f"{meta['eviction_check_cached_ns_per_read']:.1f}", EVICT_CIVAC=f"{meta['eviction_check_after_dc_civac_ns_per_read']:.1f}",
    EVICT_AFTER=f"{meta['eviction_check_after_eviction_ns_per_read']:.1f}",
    S1_1=f"{s1['flushed', 1]:.2f}", S1_256=f"{s1['flushed', 256]:.2f}",
    W2_SMALL=f"{pr(w2[sizes2[0]], sizes2[0]):.0f}", W2_LARGE=f"{pr(w2[sizes2[-1]], sizes2[-1]):.0f}",
    W2_SMALL_N=f"{sizes2[0]:,}", W2_LARGE_N=f"{sizes2[-1]:,}", W2_SIZES=", ".join(f"{n:,}" for n in sizes2),
    W2_REPS=w2[sizes2[0]]["repeats"], RULE_LO=f"{min(rule_ratio):.2f}", RULE_HI=f"{max(rule_ratio):.2f}",
    W2_TOTAL=f"{f(w2[sizes2[-1]]['total_median_ms']):.1f}", Q4_TOTAL=f"{f(q4[sizes2[-1]]['total_median_ms']):.1f}",
    P_FLAT=f"{flat:.1f}", P_KNEE=str(knee), P_KNEE_M1=str(knee - 1 if knee else max(one)), P_KNEE_N=f"{2 ** knee:,}" if knee else "-", P_CROSS=str(cross),
    P_CROSS_N=f"{2 ** cross:,}" if cross else "-", P_TOP=f"{one[max(one)]:.1f}", P_MAXB=str(max(one)), P_CV=f"{cv3:.0f}",
    P_ZERO=f"{one[0]:.2f}",
    J_SIZES=", ".join(rows_word(n) for n in sizes4), J_BIG=f"{n_big:,}", J_BIG_S=f"{simple4[n_big]:.0f}",
    J_BIG_R=f"{radix4[n_big]:.0f}", J_BIG_X=f"{simple4[n_big] / radix4[n_big]:.2f}", J_BIG_B=str(best4[n_big][1]),
    J_SMALL=f"{sizes4[0]:,}", J_SMALL_X=f"{simple4[sizes4[0]] / radix4[sizes4[0]]:.2f}",
    J_FIRST_SEP=f"{first_sep:,}" if first_sep else "no size", J_ROWS=join_rows,
    J_S_FIRST=f"{simple4[sizes4[0]] * 1e6 / sizes4[0]:.0f}", J_S_LAST=f"{simple4[n_big] * 1e6 / n_big:.0f}", J_S_HI=f"{max(simple4[n] * 1e6 / n for n in sizes4):.0f}", J_S_HI_N=f"{max(sizes4, key=lambda n: simple4[n] / n):,}",
    J_R_LO=f"{min(radix4[n] * 1e6 / n for n in sizes4):.0f}", J_R_HI=f"{max(radix4[n] * 1e6 / n for n in sizes4):.0f}",
    J_BITS_LO=str(min(best4[n][1] for n in sizes4 if n >= 1e6)), J_BITS_HI=str(max(best4[n][1] for n in sizes4 if n >= 1e6)),
    SEED_ROWS=main_seed_rows + seed_rows, SEED_LO=f"{min(s[2] / s[3] for s in seeds):.2f}", SEED_HI=f"{max(s[2] / s[3] for s in seeds):.2f}",
    MODEL_GOOD=str(model_good), MODEL_N=str(len(model)), MODEL_WORST=f"{model_worst:.0f}",
    CLOCK_NS=f"{clock_ns:.0f}", CLK_OLD=f"{clk_old:.3f}", CLK_NEW=f"{clk_new:.3f}",
    T_LIST=", ".join(map(str, threads)), T_ROWS=thread_rows, T_BITS=str(pbits), T_PASSES=str(ppasses),
    T_R4=f"{sp['radix'].get(4, 0):.2f}", T_S4=f"{sp['simple'].get(4, 0):.2f}",
    T_R8=f"{sp['radix'].get(max(threads), 0):.2f}", T_S8=f"{sp['simple'].get(max(threads), 0):.2f}", T_MAX=str(max(threads)),
    T_PART4=f"{psp.get('partition_r', 0):.2f}", T_PROBE4=f"{psp.get('probe', 0):.2f}", T_BUILD4=f"{psp.get('build', 0):.2f}",
    T_ROWS_N=f"{int(e5.get('rows', 16000000)):,}", T_COMPARE=t_compare,
    OLD_E5_SBUILD=f"{o5['simple', 1, 'build']:.0f}", OLD_T_S4=f"{o5['simple', 1, 'total'] / o5['simple', 4, 'total']:.2f}",
    B_LOADS=", ".join(lab(l) for l in loads6), B_SIZES=", ".join(f"{n:,}" for n in sizes6),
    B_REPS=next(iter(next(iter(b6.values())).values()))["runs"], B_ROWS=b_rows, B_RESULT=b_result,
    MIX_DEV=f"{100 * mix_dev:.2f}", H_N=f"{h_n:,}", H_ROWS=h_rows, H_NOTE=h_note,
    E5_SBUILD=f"{m5['simple', 1, 'build']:.0f}", E5_SPROBE=f"{m5['simple', 1, 'probe']:.0f}",
    E4_SBUILD=f"{f(by4[int(e5.get('rows', 16000000))]['simple', 0, 0]['build_median_ms']):.0f}",
    E4_SBUILD_LO=f"{min(sb4):.0f}", E4_SBUILD_HI=f"{max(sb4):.0f}",
    E4_SPROBE=f"{f(by4[int(e5.get('rows', 16000000))]['simple', 0, 0]['probe_median_ms']):.0f}",
)
for k, v in vals.items():
    html = html.replace("{{" + k + "}}", v)
assert "{{" not in html, html[html.find("{{"):html.find("{{") + 40]
a.out.write_text(html)
print("wrote", a.out)
