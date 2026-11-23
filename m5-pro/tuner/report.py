#!/usr/bin/env python3
"""Draws report.pdf for one tuner run. Standalone: needs only matplotlib.

Usage:
    python3 report.py RUN_DIR [OTHER_RUN_DIR ...]
Writes RUN_DIR/report.pdf. Other run folders (for example the tuner's run on
another machine) add a comparison page. All runs must have the same spec
version (SPEC.md): results from different versions are never compared.
"""
import csv
import math
import sys
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#6250d6"]
INK, INK2, INK3, GRID, RULE = "#1d1d1b", "#55544f", "#8a8984", "#e6e5e1", "#d4d3ce"
NOCUT = "#8a8984"
A4 = (8.27, 11.69)
LOADS = [0.125, 0.25, 0.5, 1, 2, 4]

plt.rcParams.update({
    "font.size": 8.5, "axes.edgecolor": RULE, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.6, "axes.titlesize": 9.5, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.titlecolor": INK, "legend.frameon": False, "pdf.fonttype": 42,
})


def kv(path):
    if not Path(path).exists():
        return {}
    return dict(line.rstrip("\n").split("=", 1) for line in open(path) if "=" in line)


def rows(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def load_label(x):
    x = float(x)
    return f"1/{round(1 / x)}" if x < 1 else f"{x:g}"


def knobs_label(b, p, load, short=False):
    b, p = int(b), int(p)
    cut = "no cutting" if b == 0 else f"{b} bits, {p} pass" + ("es" if p > 1 else "")
    return f"{cut}, {load_label(load)}" + ("" if short else " rows per bucket")


def code_label(code, short=False):
    b, p, load = code.split("/")
    return knobs_label(b, p, load, short)


def size_label(n):
    n = int(n)
    e = round(math.log2(n)) if n > 0 else 0
    return f"{n:,} (2^{e})" if n > 0 and 1 << e == n else f"{n:,}"


def seconds_label(s):
    s = float(s)
    return f"{int(s // 60)} min {round(s % 60)} s" if s >= 60 else f"{s:.0f} s"


class Run:
    def __init__(self, folder):
        self.dir = Path(folder)
        self.machine = kv(self.dir / "machine.txt")
        self.rule = kv(self.dir / "rule.txt")
        self.stages = rows(self.dir / "stages.csv")
        self.best = rows(self.dir / "best.csv")
        self.trials = rows(self.dir / "trials.csv")
        self.spec = self.machine.get("spec", "?")
        self.train = [r for r in self.best if r["role"] == "train"]
        self.sizes = [int(r["rows_per_input"]) for r in self.train]

    def stage(self, n, name):
        return [r for r in self.stages if int(r["rows_per_input"]) == n and r["stage"] == name]


class Page:
    """A text page: lines are placed top-down with a running y position."""

    def __init__(self, pdf):
        self.pdf, self.fig, self.y = pdf, plt.figure(figsize=A4), 0.95

    def text(self, s, size=9.5, color=INK, weight="normal", width=98, gap=0.008):
        for line in textwrap.wrap(s, width) or [""]:
            self.fig.text(0.08, self.y, line, fontsize=size, color=color, weight=weight, va="top")
            self.y -= size / 72 / A4[1] * 1.5
        self.y -= gap

    def table(self, header, body, widths, size=8.5):
        h = (len(body) + 1) * 0.024
        ax = self.fig.add_axes([0.08, self.y - h, 0.84, h])
        ax.axis("off")
        t = ax.table(cellText=body, colLabels=header, colWidths=widths, loc="upper left", cellLoc="left")
        t.auto_set_font_size(False)
        t.set_fontsize(size)
        t.scale(1, 1.45)
        for (r, _), cell in t.get_celld().items():
            cell.visible_edges = "B"
            cell.set_edgecolor(RULE)
            if r == 0:
                cell.set_text_props(color=INK2, weight="bold")
        self.y -= h + 0.02

    def close(self):
        self.pdf.savefig(self.fig)
        plt.close(self.fig)


def page_summary(pdf, run):
    m, rule = run.machine, run.rule
    pg = Page(pdf)
    pg.text("RADIX JOIN TUNER", 8, SERIES[0], "bold", gap=0.004)
    pg.text(f"Radix hash join tuned on {m.get('cpu', '?')}", 17, INK, "bold", 55)
    pg.text(f"Spec {run.spec} · profile {m.get('profile')} · {m.get('date')} · {m.get('os')} · "
            f"{int(m.get('ram_bytes', 0)) / 2 ** 30:.0f} GB RAM · {seconds_label(m.get('seconds_total', 0))} in total",
            8.5, INK2, gap=0.02)
    if m.get("profile") != "full":
        pg.text("Quick profile: this only checks that everything runs. Its numbers are not results.", 9.5,
                SERIES[1], "bold")
    pg.text("The tuner ran the radix hash join of Boncz, Manegold and Kersten on this machine with many settings of "
            "three knobs: radix bits (both tables are cut into 2^bits pieces before joining), passes (in how many "
            "rounds they are cut) and rows per bucket (how full each piece's hash table is). The loss is the time of "
            "one whole join divided by the rows per table: lower is better. The tuner never read this machine's "
            "cache sizes; the timings decide. Everything else is frozen by SPEC.md, version " + run.spec + ".")
    pg.y -= 0.01
    pg.text("Fastest setting per table size", 11, INK, "bold", gap=0.004)
    body = []
    for r in run.train:
        ties = [t for t in r["tied_settings"].split(";") if t]
        body.append([f"{int(r['rows_per_input']):,}", knobs_label(r["bits"], r["passes"], r["rows_per_bucket"]),
                     f"{float(r['median_ns_per_row']):.2f}", f"{float(r['gain_vs_no_partitioning']):.2f}x",
                     str(len(ties))])
    pg.table(["Rows per table", "Fastest setting", "ns per row", "vs no cutting", "Tied"], body,
             [0.17, 0.46, 0.12, 0.14, 0.08])
    pg.text("ns per row: median of 11 runs in the final race. vs no cutting: how many times as fast as one hash table "
            "on all of R. Tied: other settings whose fastest run beat the winner's slowest run.", 8, INK2, width=118)
    edges = {r["range_edge"] for r in run.train if r.get("range_edge")}
    edge_sizes = [f"{int(r['rows_per_input']):,}" for r in run.train if r.get("range_edge")]
    for e in sorted(edges):
        where = "every size" if len(edge_sizes) == len(run.train) else ", ".join(edge_sizes) + " rows"
        pg.text(f"Range edge at {where}: {e}. A value outside the frozen range might be faster; widening the range "
                "needs spec version 2.", 8.5, SERIES[1], width=110)
    pg.y -= 0.01
    pg.text("What the tuning bought, and how sharp the minimum is", 11, INK, "bold", gap=0.004)
    for r in run.train:
        n = int(r["rows_per_input"])
        a = run.stage(n, "A")
        a_best = min(a, key=lambda x: float(x["median_ns_per_row"]))
        race = {(x["bits"], x["passes"], x["rows_per_bucket"]): float(x["median_ns_per_row"]) for x in run.stage(n, "C")}
        a_in_race = race.get((a_best["bits"], a_best["passes"], a_best["rows_per_bucket"]))
        low = {}
        for x in a:
            b = int(x["bits"])
            low[b] = min(low.get(b, 1e18), float(x["median_ns_per_row"]))
        floor_ = min(low.values())
        near = sorted(b for b, v in low.items() if v <= floor_ * 1.05)
        win = float(r["median_ns_per_row"])
        text = f"{n:,} rows: radix bits from {near[0]} to {near[-1]} all came within 5% of the lowest point of the curve"
        if len(near) != near[-1] - near[0] + 1:
            text += f" (not every value in between: {', '.join(map(str, near))})"
        text += "."
        if a_in_race and not (a_best["bits"] == r["bits"] and a_best["passes"] == r["passes"]
                              and a_best["rows_per_bucket"] == r["rows_per_bucket"]):
            text += (f" The best setting at 1/2 row per bucket ({knobs_label(a_best['bits'], a_best['passes'], a_best['rows_per_bucket'], True)})"
                     f" took {a_in_race:.2f} ns per row in the final race; the tuned one {win:.2f}: "
                     f"{a_in_race / win:.2f} times as fast.")
        pg.text(text, 9)
    pg.y -= 0.01
    pg.text("The learned rule", 11, INK, "bold", gap=0.004)
    if rule.get("rows_per_partition", "none") != "none":
        t = float(rule["rows_per_partition"])
        load = float(rule["rows_per_bucket"])
        kib = t * 8 / 1024
        table_kib = (t * 4 + 4 * 2 ** math.ceil(math.log2(max(1.0, t / load)))) / 1024
        pg.text(f"Aim for about {t:,.0f} rows of R per piece: bits = round(log2(rows per table / {t:,.0f})), with "
                f"{rule['passes']} pass{'es' if int(rule['passes']) > 1 else ''} and {load_label(load)} rows per bucket. "
                f"A piece of R is then about {kib:,.0f} KiB, and its hash table (buckets and chain links) about "
                f"{table_kib:,.0f} KiB. The tuner found this size by timing, not from a cache size.")
    else:
        pg.text("No cutting won at every training size, so the rule is: do not cut.")
    if rule.get("test_rule"):
        tied = rule.get("test_tied") == "yes"
        ratio = float(rule["test_rule_vs_best"])
        same = rule["test_rule"] == rule["test_best"]
        pg.text(f"Held-out check at {int(rule['test_rows']):,} rows, a size the rule was not built from: the rule chose "
                f"{code_label(rule['test_rule'])}. "
                + ("It was also the fastest setting measured there." if same else
                   f"The fastest measured was {code_label(rule['test_best'])}; the rule's choice took {ratio:.3f} times "
                   f"its time ({'tied: their run ranges overlap' if tied else 'slower, not tied'})."))
    elif rule.get("test_rows"):
        pg.text(f"Held-out check: {rule['test_rows']}.")
    pg.y -= 0.01
    pg.text("Time spent", 11, INK, "bold", gap=0.004)
    parts = [f"{int(k[8:]):,} rows: {seconds_label(v)}" for k, v in m.items()
             if k.startswith("seconds_") and k[8:].isdigit()]
    if rule.get("test_seconds"):
        parts.append(f"held-out check: {seconds_label(rule['test_seconds'])}")
    pg.text("; ".join(parts) + f". Total: {seconds_label(m.get('seconds_total', 0))}.")
    pg.text(f"Thread: {m.get('thread')}. Compiler: {m.get('compiler')}. Files: {run.dir}/", 8, INK2, width=118)
    pg.close()


def page_curves(pdf, run):
    fig, axes = plt.subplots(len(run.sizes), 1, figsize=A4, squeeze=False)
    fig.subplots_adjust(left=0.1, right=0.95, top=0.885, bottom=0.06, hspace=0.42)
    fig.text(0.08, 0.955, "Stage A: the loss curve", fontsize=14, weight="bold", color=INK)
    fig.text(0.08, 0.935, "\n".join(textwrap.wrap(
        "Loss for every number of radix bits and passes, at 1/2 row per bucket. Lines: median of 3 rounds; bands: "
        "fastest to slowest run. Grey diamond: no cutting. Ring: lowest point of the curve.", 110)),
        fontsize=8.5, color=INK2, va="top")
    for ax, n in zip(axes[:, 0], run.sizes):
        st = run.stage(n, "A")
        top = 0
        for p in (1, 2, 3):
            pts = sorted((int(r["bits"]), float(r["median_ns_per_row"]), float(r["min_ns_per_row"]),
                          float(r["max_ns_per_row"])) for r in st if int(r["passes"]) == p)
            if not pts:
                continue
            x, y, lo, hi = zip(*pts)
            top = max(top, max(y))
            ax.fill_between(x, lo, hi, color=SERIES[p - 1], alpha=0.15, lw=0)
            ax.plot(x, y, "-o", color=SERIES[p - 1], ms=2.8, lw=1.5, label=f"{p} pass" + ("es" if p > 1 else ""))
        for r in st:
            if int(r["bits"]) == 0:
                med = float(r["median_ns_per_row"])
                top = max(top, med)
                ax.plot([0], [med], "D", color=NOCUT, ms=5, label="no cutting", zorder=3)
        best = min(st, key=lambda r: float(r["median_ns_per_row"]))
        bx, by = int(best["bits"]), float(best["median_ns_per_row"])
        ax.plot([bx], [by], "o", ms=12, mfc="none", mec=INK, mew=1.2, zorder=4)
        ax.annotate(f"lowest: {knobs_label(best['bits'], best['passes'], best['rows_per_bucket'], True)}\n"
                    f"{by:.2f} ns per row", (bx, by), xytext=(0, -26), textcoords="offset points", ha="center",
                    va="top", fontsize=8, color=INK)
        bmax = max(int(r["bits"]) for r in st)
        ax.set_xticks(range(0, bmax + 1, 2 if bmax > 12 else 1))
        ax.set_ylim(0, top * 1.1)
        ax.set_xlim(-0.6, bmax + 0.6)
        ax.set_title(f"{size_label(n)} rows per table")
        ax.set_xlabel("radix bits (pieces = 2^bits)")
        ax.set_ylabel("ns per input row")
    axes[0, 0].legend(loc="upper right", ncol=4, fontsize=8)
    pdf.savefig(fig)
    plt.close(fig)


def page_load_and_race(pdf, run):
    fig, axes = plt.subplots(len(run.sizes), 2, figsize=A4, squeeze=False, gridspec_kw={"width_ratios": [1, 1.3]})
    fig.subplots_adjust(left=0.08, right=0.9, top=0.885, bottom=0.06, hspace=0.45, wspace=0.62)
    fig.text(0.08, 0.955, "Stages B and C: bucket load, then the final race", fontsize=14, weight="bold", color=INK)
    fig.text(0.08, 0.935, "\n".join(textwrap.wrap(
        "Left: the best 4 (bits, passes) pairs of stage A with every rows-per-bucket value, median of 3 rounds. "
        "Right: the best 5 of stage B plus two references (no cutting, and stage A's winner), 11 rounds each. "
        "Dots: single runs; black bar: median.", 112)), fontsize=8.5, color=INK2, va="top")
    for i, n in enumerate(run.sizes):
        ax = axes[i, 0]
        st = run.stage(n, "B")
        pairs = []
        for r in sorted(st, key=lambda r: float(r["median_ns_per_row"])):
            if (r["bits"], r["passes"]) not in pairs:
                pairs.append((r["bits"], r["passes"]))
        for j, (b, p) in enumerate(pairs):
            pts = sorted((math.log2(float(r["rows_per_bucket"])), float(r["median_ns_per_row"])) for r in st
                         if (r["bits"], r["passes"]) == (b, p))
            x, y = zip(*pts)
            label = "no cutting" if int(b) == 0 else f"{b} bits, {p} pass" + ("es" if int(p) > 1 else "")
            ax.plot(x, y, "-o", color=SERIES[j % len(SERIES)], ms=2.8, lw=1.5, label=label)
        ax.set_xticks([math.log2(v) for v in LOADS], [load_label(v) for v in LOADS])
        ax.set_xlabel("rows per bucket")
        ax.set_ylabel("ns per input row")
        ax.set_title(f"Stage B · {int(n):,} rows")
        ax.legend(fontsize=7, loc="best")

        ax = axes[i, 1]
        race = sorted(run.stage(n, "C"), key=lambda r: float(r["median_ns_per_row"]))
        win_max = float(race[0]["max_ns_per_row"])
        for k, r in enumerate(race):
            vals = [float(t["ns_per_row"]) for t in run.trials if int(t["rows_per_input"]) == n and t["stage"] == "C"
                    and (t["bits"], t["passes"], t["rows_per_bucket"]) == (r["bits"], r["passes"], r["rows_per_bucket"])]
            tied = k > 0 and float(r["min_ns_per_row"]) <= win_max
            color = SERIES[0] if k == 0 else SERIES[0] if tied else INK3
            ax.scatter(vals, [k] * len(vals), s=9, color=color, alpha=0.55, lw=0, zorder=3)
            med = float(r["median_ns_per_row"])
            ax.plot([med, med], [k - 0.32, k + 0.32], color=INK, lw=2, zorder=4)
            note = "fastest" if k == 0 else "tied" if tied else ""
            if note:
                ax.annotate(note, (1.0, k), xycoords=("axes fraction", "data"), xytext=(4, 0),
                            textcoords="offset points", va="center", fontsize=7.5, color=SERIES[0])
        ax.set_yticks(range(len(race)), [knobs_label(r["bits"], r["passes"], r["rows_per_bucket"], True)
                                         for r in race], fontsize=7.5)
        ax.set_ylim(len(race) - 0.5, -0.5)
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("ns per input row")
        ax.set_title(f"Stage C · {int(n):,} rows")
    pdf.savefig(fig)
    plt.close(fig)


def page_heldout(pdf, run):
    st = [r for r in run.stages if r["stage"] == "T"]
    rule = run.rule
    if not st or not rule.get("test_rule"):
        return
    n = int(st[0]["rows_per_input"])
    pg = Page(pdf)
    pg.text("Held-out check", 14, INK, "bold", gap=0.004)
    pg.text(f"The rule was built from the training sizes only. Here it is tested at {size_label(n)} rows per table: "
            "its own choice, the settings with up to 3 bits fewer or more (same passes and rows per bucket), and no "
            "cutting. 5 rounds each; the band runs from the fastest to the slowest run, so one slow run makes a spike. "
            "Like a test set in machine learning, this shows whether the rule works on a size "
            "it has not seen.", 9, INK2)
    ax = pg.fig.add_axes([0.1, pg.y - 0.36, 0.85, 0.34])
    cut = sorted((int(r["bits"]), float(r["median_ns_per_row"]), float(r["min_ns_per_row"]),
                  float(r["max_ns_per_row"])) for r in st if int(r["bits"]) > 0)
    x, y, lo, hi = zip(*cut)
    ax.fill_between(x, lo, hi, color=SERIES[0], alpha=0.15, lw=0)
    ax.plot(x, y, "-o", color=SERIES[0], ms=3, lw=1.5, label=f"{rule.get('passes', '1')} pass, "
            f"{load_label(rule.get('rows_per_bucket', 0.5))} rows per bucket")
    top = max(hi)
    for r in st:
        if int(r["bits"]) == 0:
            med = float(r["median_ns_per_row"])
            top = max(top, med)
            ax.axhline(med, color=NOCUT, lw=1.2, ls="--")
            ax.annotate(f"no cutting: {med:.2f} ns per row", (x[0] - 0.4, med), xytext=(0, 4),
                        textcoords="offset points", fontsize=8, color=INK2)
    rb = int(rule["test_rule"].split("/")[0])
    bb = int(rule["test_best"].split("/")[0])
    med_of = {int(r["bits"]): float(r["median_ns_per_row"]) for r in st}
    ax.plot([rb], [med_of[rb]], "o", ms=13, mfc="none", mec=SERIES[1], mew=1.6, label="the rule's choice", zorder=4)
    ax.plot([bb], [med_of[bb]], "o", ms=8, mfc="none", mec=INK, mew=1.2, label="fastest measured", zorder=5)
    ax.set_ylim(0, top * 1.12)
    ax.set_xticks([b for b in sorted(med_of) if b > 0])
    ax.set_xlim(x[0] - 0.5, x[-1] + 0.5)
    ax.set_xlabel("radix bits")
    ax.set_ylabel("ns per input row")
    ax.legend(loc="lower right", fontsize=8)
    pg.y -= 0.40
    ratio = float(rule["test_rule_vs_best"])
    pg.text(f"Result: the rule's choice took {ratio:.3f} times the fastest measured time"
            + (", and it was the fastest." if rule["test_rule"] == rule["test_best"] else
               f" ({'tied: their run ranges overlap' if rule.get('test_tied') == 'yes' else 'slower, not tied'}).")
            + f" Time for this check: {seconds_label(rule.get('test_seconds', 0))}.")
    pg.close()


def page_compare(pdf, runs):
    pg = Page(pdf)
    pg.text("Same tuner, different machines", 14, INK, "bold", gap=0.004)
    pg.text(f"Spec {runs[0].spec} on every machine: same code, data, knobs and search. Only the machine changes, so "
            "any difference in the fastest setting comes from the machine.", 9, INK2)
    sizes = sorted({n for r in runs for n in r.sizes})
    header = ["Rows per table"] + [f"{r.machine.get('cpu', '?')[:22]}, {r.machine.get('date', '')[:10]}"
                                   + ("" if r.machine.get("profile") == "full" else " (quick)") for r in runs]
    body = []
    for n in sizes:
        line = [f"{n:,}"]
        for r in runs:
            hit = [b for b in r.train if int(b["rows_per_input"]) == n]
            line.append(f"{knobs_label(hit[0]['bits'], hit[0]['passes'], hit[0]['rows_per_bucket'], True)}; "
                        f"{float(hit[0]['median_ns_per_row']):.1f} ns" if hit else "not run")
        body.append(line)
    body.append(["Rule: rows per piece"] + [f"{float(r.rule['rows_per_partition']):,.0f}"
                                           if r.rule.get("rows_per_partition", "none") != "none" else "no cutting"
                                           for r in runs])
    w = 0.84 / len(header)
    pg.table(header, body, [0.2] + [(1 - 0.2) / (len(header) - 1)] * (len(header) - 1) if len(header) > 1 else [w])
    pg.close()


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    runs = [Run(d) for d in sys.argv[1:]]
    if len({r.spec for r in runs}) > 1:
        sys.exit("report.py: these runs use different spec versions ("
                 + ", ".join(f"{r.dir}: {r.spec}" for r in runs) + "); they cannot be compared.")
    main_run = runs[0]
    out = main_run.dir / "report.pdf"
    with PdfPages(out) as pdf:
        page_summary(pdf, main_run)
        page_curves(pdf, main_run)
        page_load_and_race(pdf, main_run)
        page_heldout(pdf, main_run)
        if len(runs) > 1:
            page_compare(pdf, runs)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
