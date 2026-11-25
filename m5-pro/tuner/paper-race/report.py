#!/usr/bin/env python3
"""Draws report.pdf for one race run: the paper's formula against the tuner.

Usage:
    python3 report.py RUN_DIR
Reads plan.csv, plan.txt, trials.csv and machine.txt in RUN_DIR; writes
RUN_DIR/summary.csv and RUN_DIR/report.pdf. Same page style as ../report.py.
"""
import csv
import math
import statistics as st
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
ORDER = ["tuner", "paper", "paper-1-pass", "first-race", "no-cutting"]
NAMES = {"tuner": "Tuner's pick", "paper": "Paper's formula", "paper-1-pass": "Paper's bits, 1 pass",
         "first-race": "First race's version", "no-cutting": "No cutting"}
COLORS = {"tuner": SERIES[0], "paper": SERIES[1], "paper-1-pass": SERIES[2], "first-race": SERIES[3],
          "no-cutting": NOCUT}

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


def load_text(x):
    x = float(x)
    return f"1/{round(1 / x)}" if x < 1 else f"{x:g}"


def setting_text(b, p, load):
    b, p = int(b), int(p)
    cut = "no cutting" if b == 0 else f"{b} bits, {p} pass" + ("es" if p > 1 else "")
    return f"{cut}, {load_text(load)}"


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


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python3 report.py RUN_DIR")
    d = Path(sys.argv[1])
    machine, plan_kv = kv(d / "machine.txt"), kv(d / "plan.txt")
    plan = rows(d / "plan.csv")
    trials = rows(d / "trials.csv")
    rounds = int(machine.get("rounds", "0"))
    sizes = sorted({int(t["rows_per_input"]) for t in trials})
    setting = {(int(p["rows_per_input"]), p["label"]): p for p in plan}
    runs, by_round = {}, {}
    for t in trials:
        n, label, ns = int(t["rows_per_input"]), t["label"], float(t["ns_per_row"])
        runs.setdefault((n, label), []).append(ns)
        by_round[(n, int(t["round"]), label)] = ns

    stats = {}
    for (n, label), v in runs.items():
        stats[(n, label)] = {"runs": len(v), "mean": st.mean(v), "sd": st.stdev(v) if len(v) > 1 else 0.0,
                             "median": st.median(v), "min": min(v), "max": max(v)}
    for n in sizes:
        tm = stats[(n, "tuner")]["mean"]
        for label in ORDER:
            s = stats[(n, label)]
            s["vs_tuner"] = s["mean"] / tm
            s["rounds_tuner_faster"] = sum(by_round[(n, r, "tuner")] < by_round[(n, r, label)]
                                           for r in range(1, rounds + 1)) if label != "tuner" else ""
    with open(d / "summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rows_per_input", "label", "bits", "passes", "rows_per_bucket", "runs", "mean_ns_per_row",
                    "sd_ns_per_row", "median_ns_per_row", "min_ns_per_row", "max_ns_per_row", "mean_vs_tuner",
                    "rounds_tuner_faster"])
        for n in sizes:
            for label in ORDER:
                p, s = setting[(n, label)], stats[(n, label)]
                w.writerow([n, label, p["bits"], p["passes"], p["rows_per_bucket"], s["runs"], f"{s['mean']:.4f}",
                            f"{s['sd']:.4f}", f"{s['median']:.4f}", f"{s['min']:.4f}", f"{s['max']:.4f}",
                            f"{s['vs_tuner']:.4f}", s["rounds_tuner_faster"]])

    ratio = {n: stats[(n, "paper")]["vs_tuner"] for n in sizes}
    passes_cost = {n: stats[(n, "paper")]["mean"] / stats[(n, "paper-1-pass")]["mean"] for n in sizes}
    bits_cost = {n: stats[(n, "paper-1-pass")]["vs_tuner"] for n in sizes}
    first = {n: stats[(n, "first-race")]["vs_tuner"] for n in sizes}
    overlap = {n: stats[(n, "paper")]["min"] <= stats[(n, "tuner")]["max"] for n in sizes}
    wins = {n: stats[(n, "paper")]["rounds_tuner_faster"] for n in sizes}
    rng = lambda m: f"{min(m.values()):.2f} to {max(m.values()):.2f}"  # noqa: E731

    if all(r > 1 for r in ratio.values()):
        headline = (f"At all {len(sizes)} sizes the tuner's pick was faster on average. The paper's formula took "
                    f"{rng(ratio)} times as long.")
    elif all(r < 1 for r in ratio.values()):
        headline = (f"At all {len(sizes)} sizes the paper's formula was faster on average: {rng(ratio)} times "
                    f"the tuner's time.")
    else:
        headline = "The faster side depends on the size: " + "; ".join(
            f"{n:,} rows: the paper took {ratio[n]:.2f} times the tuner's time" for n in sizes) + "."
    headline += (f" The tuner was faster in {sum(wins.values())} of the {rounds * len(sizes)} rounds "
                 f"({', '.join(str(wins[n]) for n in sizes)} of {rounds} at the {len(sizes)} sizes).")
    paper_passes = sorted({int(setting[(n, "paper")]["passes"]) for n in sizes})
    if paper_passes == [1]:
        breakdown = "The paper's formula gives 1 pass at every size here, so passes make no difference. "
    else:
        breakdown = (f"Where the difference comes from: cutting the paper's bits in its "
                     f"{' or '.join(map(str, paper_passes))} passes instead of 1 took {rng(passes_cost)} times as "
                     f"long. ")
    breakdown += (f"The paper's bits in 1 pass took {rng(bits_cost)} times the tuner's time. Both sides used the "
                  f"same rows per box.")

    out = d / "report.pdf"
    with PdfPages(out) as pdf:
        # ---------- page 1: what was compared, and the answer ----------
        pg = Page(pdf)
        pg.text("Paper's formula against the tuner", size=17, weight="bold", gap=0.004)
        pg.text(f"{machine.get('cpu', '?')} · {machine.get('date', '?')} · {rounds} rounds · tuner run "
                f"{plan_kv.get('tuner_run', '?')} · tuner spec {machine.get('tuner_spec', '?')}", size=9, color=INK2,
                gap=0.02)
        pg.text("Both sides run the same join code (the tuner's own source file), on the same data, at the same "
                "sizes, on the same machine, in the same rounds. Only the settings differ. Every setting got 1 "
                f"untimed warm-up join, then {rounds} timed joins, one per round. Each round ran every setting of its "
                "size once, in a newly shuffled order. Times are ns per row: the time of one whole join divided by the "
                f"rows in one table. 'Average' is the mean of the {rounds} timed joins. Every join's answer was "
                "checked against the known answer.", gap=0.016)
        pg.text("The answer", size=12, weight="bold")
        pg.text(headline, gap=0.006)
        pg.text(breakdown, gap=0.016)
        pg.text("Result: tuner's pick against the paper's formula", size=11, weight="bold")
        pg.table(["Rows per table", "Tuner, average", "Paper, average", "Paper ÷ tuner", "Tuner faster",
                  "Ranges overlap"],
                 [[f"{n:,}", f"{stats[(n, 'tuner')]['mean']:.2f} ns", f"{stats[(n, 'paper')]['mean']:.2f} ns",
                   f"{ratio[n]:.2f}x", f"{wins[n]} of {rounds}", "yes (tie)" if overlap[n] else "no"]
                  for n in sizes], [0.16, 0.17, 0.17, 0.16, 0.16, 0.18])
        pg.text("The settings (bits, passes, rows per box)", size=11, weight="bold")
        pg.table(["Rows per table"] + [NAMES[k] for k in ORDER],
                 [[f"{n:,}"] + [setting_text(setting[(n, k)]["bits"], setting[(n, k)]["passes"],
                                             setting[(n, k)]["rows_per_bucket"]) for k in ORDER] for n in sizes],
                 [0.13, 0.17, 0.18, 0.18, 0.18, 0.16], size=7.5)
        pg.text("Tuner's pick: its winner at the sizes it learned from (1,048,576, 4,194,304 and 16,777,216 rows) "
                "and its learned rule elsewhere. Paper's formula: bits from formula 1 (page 63: cut until one piece "
                "plus its hash table, 12 bytes per row, fits in the first cache), passes from formula 2 (page 60: "
                "at most 6 bits per pass, from the paper's 64-entry page-address list). The paper gives no rule for "
                "rows per box, so its side uses the tuner's. Paper's bits, 1 pass: what formula 2 gives if this "
                "machine's page-address list holds at least 2^bits entries; macOS does not report the number. "
                "First race's version: as in the race of 23 November, 20 bytes per row and 1/2 row per box.",
                size=8.5, color=INK2)
        pg.close()

        # ---------- page 2: averages and ratios ----------
        fig = plt.figure(figsize=A4)
        fig.text(0.08, 0.95, "Averages and ratios", fontsize=14, weight="bold", color=INK, va="top")
        ax = fig.add_axes([0.1, 0.56, 0.82, 0.32])
        width = 0.16
        for i, label in enumerate(ORDER):
            xs = [j + (i - 2) * width for j in range(len(sizes))]
            means = [stats[(n, label)]["mean"] for n in sizes]
            lo = [stats[(n, label)]["mean"] - stats[(n, label)]["min"] for n in sizes]
            hi = [stats[(n, label)]["max"] - stats[(n, label)]["mean"] for n in sizes]
            ax.bar(xs, means, width * 0.88, color=COLORS[label], label=NAMES[label], zorder=2,
                   hatch="//" if label == "no-cutting" else None, edgecolor="white", linewidth=0.6)
            ax.errorbar(xs, means, yerr=[lo, hi], fmt="none", ecolor=INK2, elinewidth=0.8, capsize=1.8, zorder=3)
        ax.set_xticks(range(len(sizes)), [f"{n:,}" for n in sizes])
        ax.set_xlabel("rows per table")
        ax.set_ylabel(f"ns per row (average of {rounds})")
        ax.set_title(f"Average time per row; thin bars show the fastest and slowest of the {rounds} runs")
        ax.legend(ncol=3, fontsize=7.5, loc="upper left", bbox_to_anchor=(0, -0.13))
        ax.grid(axis="x", visible=False)

        ax = fig.add_axes([0.1, 0.1, 0.62, 0.3])
        ax.axhline(1, color=COLORS["tuner"], linewidth=1.5)
        top = max(stats[(n, k)]["vs_tuner"] for n in sizes for k in ORDER) * 1.12
        ends = [(1.0, "Tuner's pick (1.00x)")]
        for label in ORDER[1:]:
            v = [stats[(n, label)]["vs_tuner"] for n in sizes]
            ax.plot(range(len(sizes)), v, marker="o", markersize=5, color=COLORS[label], linewidth=2,
                    linestyle="--" if label == "no-cutting" else "-", markeredgecolor="white", markeredgewidth=1)
            ends.append((v[-1], f"{NAMES[label]} ({v[-1]:.2f}x)"))
        ends.sort()
        gap, placed = top * 0.055, []
        for y, name in ends:  # keep end labels at least `gap` apart, in value order
            y = max(y, placed[-1] + gap) if placed else y
            placed.append(y)
            ax.text(len(sizes) - 1 + 0.08, y, name, color=INK2, fontsize=7.5, va="center")
        ax.set_xticks(range(len(sizes)), [f"{n:,}" for n in sizes])
        ax.set_xlim(-0.2, len(sizes) - 1 + 0.15)
        ax.set_ylim(0, top)
        ax.set_xlabel("rows per table")
        ax.set_ylabel("times as long as the tuner's pick")
        ax.set_title("Average time divided by the tuner's average time at the same size")
        pdf.savefig(fig)
        plt.close(fig)

        # ---------- page 3: every timed join, and the full table ----------
        fig = plt.figure(figsize=A4)
        fig.text(0.08, 0.95, f"Every timed join ({rounds} per setting)", fontsize=14, weight="bold", color=INK,
                 va="top")
        cols = min(2, len(sizes))
        nrows = math.ceil(len(sizes) / cols)
        for k, n in enumerate(sizes):
            r, c = divmod(k, cols)
            ax = fig.add_axes([0.1 + c * 0.45, 0.66 - r * 0.24, 0.37, 0.19])
            for i, label in enumerate(ORDER):
                v = runs[(n, label)]
                xs = [i + (j - (len(v) - 1) / 2) * 0.035 for j in range(len(v))]
                ax.scatter(xs, v, s=11, color=COLORS[label], zorder=3, edgecolors="white", linewidths=0.4)
                m = stats[(n, label)]["mean"]
                ax.plot([i - 0.3, i + 0.3], [m, m], color=INK, linewidth=1, zorder=4)
            ax.set_xticks(range(len(ORDER)), ["tuner", "paper", "paper\n1 pass", "first\nrace", "no\ncutting"],
                          fontsize=7)
            ax.set_ylim(0, max(max(runs[(n, k2)]) for k2 in ORDER) * 1.1)
            ax.set_title(f"{n:,} rows per table", fontsize=8.5)
            ax.set_ylabel("ns per row")
            ax.grid(axis="x", visible=False)
        fig.text(0.08, 0.66 - (nrows - 1) * 0.24 - 0.055, "Dots: one timed join each. Black line: the average.",
                 fontsize=8, color=INK2)
        pdf.savefig(fig)
        plt.close(fig)

        pg = Page(pdf)
        pg.text("All numbers", size=14, weight="bold", gap=0.012)
        body = []
        for n in sizes:
            for label in ORDER:
                p, s = setting[(n, label)], stats[(n, label)]
                body.append([f"{n:,}", NAMES[label], setting_text(p["bits"], p["passes"], p["rows_per_bucket"]),
                             f"{s['mean']:.2f}", f"{s['median']:.2f}", f"{s['min']:.2f} to {s['max']:.2f}",
                             f"{s['sd']:.2f}", f"{s['vs_tuner']:.2f}x",
                             "" if label == "tuner" else f"{s['rounds_tuner_faster']} of {rounds}"])
        pg.table(["Rows", "Setting", "Bits, passes, box", "Average", "Median", "Fastest–slowest",
                  "Std. dev.", "vs tuner", "Tuner faster"], body,
                 [0.1, 0.15, 0.17, 0.08, 0.08, 0.14, 0.08, 0.08, 0.1], size=7)
        pg.text("ns per row. Std. dev. (standard deviation): how far a typical run lies from the average. "
                "'Tuner faster': in how many rounds the tuner's pick beat this setting in the same round.",
                size=8, color=INK2)
        pg.close()

        # ---------- page 5: how the settings were worked out, and limits ----------
        pg = Page(pdf)
        pg.text("How every setting was worked out", size=14, weight="bold", gap=0.012)
        for line in (d / "plan.txt").read_text().splitlines():
            if line.startswith("== "):
                pg.text(line[3:], size=9.5, weight="bold", gap=0.002)
            elif line:
                pg.text(line, size=8.5, color=INK2, width=110, gap=0.0)
            else:
                pg.text("", size=4, gap=0.0)
        pg.y -= 0.02
        pg.text("What this run does and does not show", size=12, weight="bold", gap=0.006)
        for s in [
            "It compares two ways of choosing the radix join's settings. The join itself is the paper's algorithm "
            "in both cases.",
            "The paper's passes rule is filled in with its 1999 computer's 64-entry page-address list, because macOS "
            "does not report this machine's number. 'Paper's bits, 1 pass' shows the result if this machine's list "
            "is large enough for 1 pass.",
            "The paper gives no rule for rows per box; its side uses the tuner's, so that difference is not counted "
            "against the paper. 'First race's version' shows the earlier choice of 1/2.",
            "The tuner's picks come from timings on this machine with this data. The formula uses no timings. That "
            "is the difference between the two methods, and an advantage for the tuner here.",
            "One machine, one thread, one data set (unique keys, both tables the same size, half of S matching). "
            "Other programs running on the laptop at the same time slow down whichever settings run at that moment; "
            "the shuffled order spreads that over all settings.",
            f"Run time: {machine.get('seconds_total', '?')} s in total.",
        ]:
            pg.text("• " + s, size=8.5, width=104, gap=0.004)
        pg.close()
    print(f"Wrote {out} and {d / 'summary.csv'}")
    print(headline)
    print(breakdown)


if __name__ == "__main__":
    main()
