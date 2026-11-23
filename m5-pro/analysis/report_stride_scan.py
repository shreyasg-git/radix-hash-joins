"""Stride scan on the M5 Pro: check the measurements, draw the plots and build
the PDF in the same layout as reports/stride-scan.pdf (the Ryzen report).

    python3 analysis/report_stride_scan.py results/01-stride-scan --pdf reports/01-stride-scan.pdf

Validation, summary.csv and the three figures are the same as in
../analysis/analyze_scan.py. Sentences that quote numbers are computed from
the data, so they stay true for a new run.
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("results_directory", nargs="?", type=Path, default=ROOT / "results" / "01-stride-scan")
parser.add_argument("--pdf", type=Path, default=ROOT / "reports" / "01-stride-scan.pdf")
args = parser.parse_args()
OUT = args.results_directory.resolve()
FIG = OUT / "figures"
FIG.mkdir(exist_ok=True)
meta = json.loads((OUT / "benchmark_metadata.json").read_text())
line_bytes = meta["line_bytes_sysctl"]

# ---------- validation and summary (as in analyze_scan.py) ----------------
rows = list(csv.DictReader((OUT / "raw_timings.csv").open()))
strides = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
groups = defaultdict(list)
checks = defaultdict(set)
seen = set()
for r in rows:
    key = (r["condition"], int(r["stride_bytes"]))
    assert key[0] in ("flushed", "repeat_scan") and key[1] in strides
    assert int(r["reads"]) == meta["reads_per_scan"] == 200000
    assert int(r["address_span_bytes"]) == (200000 - 1) * key[1] + 1 <= meta["buffer_bytes"]
    assert int(r["selected_cache_lines"]) == min(200000, (int(r["address_span_bytes"]) + line_bytes - 1) // line_bytes)
    assert float(r["elapsed_ns"]) > 0
    identity = (int(r["round"]), *key)
    assert identity not in seen
    seen.add(identity)
    groups[key].append(float(r["elapsed_ns"]) / 1e6)
    checks[key[1]].add(int(r["checksum"]))
assert len(rows) == 2 * len(strides) * meta["repetitions"]
assert all(len(v) == 1 for v in checks.values())
assert all(len(groups[c, s]) == meta["repetitions"] for c in ("flushed", "repeat_scan") for s in strides)
stats = {}
for (condition, stride), values in sorted(groups.items()):
    v = np.array(values)
    stats[condition, stride] = dict(condition=condition, stride_bytes=stride, n=len(v),
        mean_ms=float(v.mean()), median_ms=float(np.median(v)), sample_sd_ms=float(v.std(ddof=1)),
        min_ms=float(v.min()), p05_ms=float(np.percentile(v, 5)), p95_ms=float(np.percentile(v, 95)),
        max_ms=float(v.max()), cv_percent=float(100 * v.std(ddof=1) / v.mean()),
        median_ns_per_read=float(np.median(v) * 1e6 / 200000))
with (OUT / "summary.csv").open("w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=list(next(iter(stats.values()))))
    writer.writeheader()
    writer.writerows(stats.values())
overhead = [float(r["elapsed_ns"]) for r in csv.DictReader((OUT / "timer_overhead.csv").open())]
validation = dict(recorded_scans=len(rows), groups=len(groups), samples_per_group=meta["repetitions"],
    checksums_consistent=True, bounds_valid=True, duplicate_measurements=False,
    timer_pair_median_ns=float(np.median(overhead)), timer_pair_p95_ns=float(np.percentile(overhead, 95)))
(OUT / "validation.json").write_text(json.dumps(validation, indent=2))

# ---------- figures (identical to analyze_scan.py) ------------------------
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False,
                     "axes.spines.right": False, "axes.titleweight": "bold", "figure.facecolor": "white"})
BLUE = "#185b91"; ORANGE = "#cc6b24"


def save(fig, name):
    fig.tight_layout(); fig.savefig(FIG / name, dpi=190, bbox_inches="tight"); plt.close(fig)


main = strides[:9]
fig, ax = plt.subplots(figsize=(9, 4.1))
med = np.array([stats["flushed", s]["median_ms"] for s in main])
lo = np.array([stats["flushed", s]["p05_ms"] for s in main]); hi = np.array([stats["flushed", s]["p95_ms"] for s in main])
ax.fill_between(main, lo, hi, color=BLUE, alpha=.15, label="5th to 95th percentile of runs")
ax.plot(main, med, "o-", color=BLUE, label="Median")
ax.plot(main, [stats["flushed", s]["mean_ms"] for s in main], "s--", color=ORANGE, label="Mean")
ax.set(xlabel="Stride (bytes)", ylabel="Elapsed time (ms)", title="Initial scan on this laptop | flushed target data", xlim=(0, 262))
ax.grid(alpha=.2); ax.legend(loc="upper left", fontsize=9)
save(fig, "01_primary_stride_scan.png")

fig, ax = plt.subplots(figsize=(9, 3.8))
for cond, color, label in [("flushed", BLUE, "Flushed"), ("repeat_scan", ORANGE, "Immediate repeat scan")]:
    ax.plot(strides, [stats[cond, s]["median_ms"] for s in strides], "o-", color=color, label=label)
ax.set_xscale("log", base=2); ax.set_xticks(strides, labels=[str(s) for s in strides])
ax.set(xlabel="Stride (bytes, logarithmic spacing)", ylabel="Median elapsed time (ms)",
       title=f'Cache preparation comparison | {meta["repetitions"]} runs per point')
ax.grid(alpha=.2); ax.legend(fontsize=9)
save(fig, "02_cache_preparation.png")

fig, ax = plt.subplots(figsize=(9, 3.8))
ax.boxplot([groups["flushed", s] for s in strides], tick_labels=[str(s) for s in strides],
           patch_artist=True, boxprops={"facecolor": "#dce9f2"}, medianprops={"color": BLUE})
ax.set_yscale("log")
ax.set(xlabel="Stride (bytes)", ylabel="Elapsed time (ms, logarithmic scale)",
       title="Run variability | flushed scans with all outliers retained")
ax.grid(axis="y", alpha=.2)
save(fig, "03_run_variability.png")

# ---------- machine facts from system.txt --------------------------------
sysinfo = {}
if (OUT / "system.txt").exists():
    for line in (OUT / "system.txt").read_text().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            sysinfo[k.strip()] = v.strip()
cpu = meta.get("cpu", sysinfo.get("machdep.cpu.brand_string", "Apple silicon"))
l1 = int(sysinfo.get("hw.perflevel0.l1dcachesize", 0)) // 1024
l2 = int(sysinfo.get("hw.perflevel0.l2cachesize", 0)) // (1024 * 1024)
per_l2 = sysinfo.get("hw.perflevel0.cpusperl2", "4")
p_cores = sysinfo.get("hw.perflevel0.physicalcpu", "4")
e_cores = sysinfo.get("hw.perflevel1.physicalcpu", "6")

# ---------- sentences computed from the data ------------------------------
f = lambda cond, s, field="median_ms": stats[cond, s][field]
ratio = f("flushed", 256) / f("flushed", 1)
flat = [s for s in main if f("flushed", s) <= 1.25 * f("flushed", 1)]
flat_end = max(s for s in flat if all(t in flat for t in main if t <= s))
flat_level = np.median([f("flushed", s) for s in main if s <= flat_end])
jumps = [(f("flushed", b) / f("flushed", a), a, b) for a, b in zip(main, main[1:])]
_, j_from, j_to = max(jumps)
band = {s: (f("flushed", s, "p95_ms") - f("flushed", s, "p05_ms")) / f("flushed", s) for s in main}
widest = max(band, key=band.get)
gain = {s: 1 - f("repeat_scan", s) / f("flushed", s) for s in strides}
best_gain = max(gain, key=gain.get)
close = [s for s in strides if abs(gain[s]) < 0.05 and s > 16]
same_lines = line_bytes, 2 * line_bytes
spacing_ratio = f("flushed", same_lines[1]) / f("flushed", same_lines[0])
lines_mib = 200000 * line_bytes / 2**20
span_1024 = ((200000 - 1) * 1024 + 1) / 2**20


def ms(x, d=3):
    return f"{x:.{d}f} ms"


# ---------- PDF in the layout of reports/stride-scan.pdf ------------------
SLATE = colors.Color(.392157, .454902, .545098)
INK = colors.Color(.2, .254902, .333333)
HEADER_BG = colors.Color(.862745, .913725, .94902)
GRID = colors.Color(.85098, .85098, .85098)
S = {
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=23, leading=27, spaceAfter=4),
    "deck": ParagraphStyle("deck", fontName="Helvetica", fontSize=11, leading=14, textColor=SLATE, spaceAfter=9),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=13, spaceAfter=6),
    "h": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=16, leading=19, spaceBefore=3, spaceAfter=6),
    "cap": ParagraphStyle("cap", fontName="Helvetica", fontSize=8.3, leading=10, spaceBefore=2, spaceAfter=7),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=11),
}
story = []
p = lambda text, style="body": story.append(Paragraph(text, S[style]))


def picture(name, width):
    w, h = ImageReader(str(FIG / name)).getSize()
    story.append(Image(str(FIG / name), width=width, height=width * h / w))


p("Initial sequential scan experiment", "title")
p(f"Radix hash join project | {cpu} MacBook Pro measurements", "deck")
p(f"We wanted to see how the gap between memory reads changes the time a scan takes. Every scan read 200,000 "
  f"bytes, one at a time. After clearing the data from cache, the scan took about {ratio:.0f} times as long at "
  f"stride 256 as it did at stride 1.")
p("How we ran the test", "h")
p("We filled a 256 MiB array with random-looking byte values that we could reproduce. The stride is the gap "
  "between reads. For example, at stride 8 the program reads bytes 0, 8, 16 and so on. We tested 11 strides "
  "from 1 to 1024 bytes, doubling the gap each time.")
p(f"<b>Flushed scan.</b> Before starting the timer, we removed the data the scan would read from the CPU caches. "
  f"On this Mac we did that by reading a separate 512 MiB buffer, far larger than every cache. Apple's cache-flush "
  f"instruction, the counterpart of the one used on the Ryzen laptop, had no measurable effect for a normal "
  f"program: in a check before the benchmark, random reads took "
  f"{meta['eviction_check_cached_ns_per_read']:.1f} ns before it and "
  f"{meta['eviction_check_after_dc_civac_ns_per_read']:.1f} ns after it, but "
  f"{meta['eviction_check_after_eviction_ns_per_read']:.1f} ns after reading the large buffer. As the scan ran, "
  f"the CPU could bring the data back into cache.")
p("<b>Immediate repeat scan.</b> We read the same positions once, then timed a second scan straight away. Some "
  "of the data from the first scan could still be in cache.")
p(f"We ran three warmup rounds and then recorded {meta['repetitions']} runs for each stride and method. We "
  f"changed the order each round. macOS cannot tie a program to one processor, so the program asked for the "
  f"highest priority class, which runs on the performance cores. The timer covered reading and adding the "
  f"bytes. We checked the sum after each scan, and all {len(rows)} recorded scans passed.")
p(f"We followed the scan setup in Section 2 of the paper [1]. The laptop used an {cpu} with {p_cores} performance "
  f"and {e_cores} efficiency cores. The operating system reports {line_bytes}-byte cache lines, {l1} KiB of L1 "
  f"data cache per performance core, and {l2} MiB of L2 cache shared by {per_l2} performance cores.")
p("How the stride affected scan time", "h")
picture("01_primary_stride_scan.png", 372)
p("Figure 1. Time for the whole scan after clearing the data from cache. The blue line shows the median, the "
  "orange line shows the average, and the shaded band covers the middle 90% of measured times.", "cap")
if spacing_ratio > 1.10:
    spacing = (f"Strides {same_lines[0]} and {same_lines[1]} touch the same number of lines, so the "
               f"{100 * (spacing_ratio - 1):.0f}% rise between them suggests that the spacing between lines also matters.")
else:
    spacing = (f"Strides {same_lines[0]} and {same_lines[1]} touch the same number of lines and took about the same "
               f"time, so once every read needs its own line the time levels off.")
p(f"<b>What we saw.</b> The median stayed near {flat_level:.3f} ms for strides 1 to {flat_end}. The biggest jump "
  f"was from {ms(f('flushed', j_from))} at stride {j_from} to {ms(f('flushed', j_to))} at stride {j_to}. At "
  f"stride 256 it reached {ms(f('flushed', 256))}. You can see this in the blue line, which climbs most sharply "
  f"between {j_from} and {j_to}. The shaded band is widest, relative to the median, at stride {widest}, so the "
  f"timings varied most there.")
p(f"<b>How to read it.</b> When reads are close together, several of them can use the same {line_bytes}-byte cache "
  f"line. At stride {line_bytes} and above, each read uses a different line. {spacing} The scan does the same "
  f"number of reads at every stride, so the extra time comes from changing how it visits memory. The chart gives "
  f"us a reason to pay attention to where the join keeps the data it looks up.")
story.append(PageBreak())

p("Scan times by stride", "h")
data = [["Stride B", "Mean ms", "Median ms", "SD ms", "P95 ms", "Min ms", "Max ms"]]
for s in main:
    x = stats["flushed", s]
    data.append([s] + [f"{x[k]:.4f}" for k in ["mean_ms", "median_ms", "sample_sd_ms", "p95_ms", "min_ms", "max_ms"]])
t = Table([[Paragraph(str(c), S["cell"]) for c in row] for row in data], colWidths=[71] * 7, hAlign="LEFT")
t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEADER_BG), ("GRID", (0, 0), (-1, -1), .5, GRID),
                       ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                       ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5), ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
story.append(t)
p("SD describes how much the times varied. P95 is the 95th percentile.", "cap")
p("Did reading the data beforehand help?", "h")
picture("02_cache_preparation.png", 380)
p("Figure 2. Median scan times for both methods. Each step along the horizontal axis doubles the stride.", "cap")
close_text = (f"At stride{'s' if len(close) > 1 else ''} {', '.join(map(str, close))} the two times were within 5% "
              f"of each other." if close else "The two methods differed by more than 5% at every stride above 16.")
p(f"<b>What we saw.</b> At stride {best_gain}, reading the data beforehand brought the median down from "
  f"{f('flushed', best_gain):.4f} ms to {f('repeat_scan', best_gain):.4f} ms, the largest saving of any stride "
  f"({100 * gain[best_gain]:.0f}%). {close_text} At stride 1024 the flushed scan took "
  f"{f('flushed', 1024):.4f} ms and the repeat scan {f('repeat_scan', 1024):.4f} ms.")
p(f"<b>How to read it.</b> Reading the data first helped at some strides. The gap between reads still had a bigger "
  f"effect on the overall time. At stride 1024, the reads span about {span_1024:.0f} MiB of addresses, but they skip "
  f"most of that space. They touch about {lines_mib:.1f} MiB of cache lines, the same amount as at stride 256. On "
  f"this Mac, emptying the cache by reading a large buffer also empties the processor's table of recently used "
  f"memory pages (the TLB), which the Ryzen's flush instruction did not. The timings show a slowdown as the reads "
  f"spread farther apart. They do not tell us exactly what caused it.")
p("What this means for our radix hash join", "h")
p("A simple hash join can jump around one large hash table as it looks for matches. Radix partitioning groups "
  "matching keys so we can join one pair of partitions at a time. The smaller hash tables give recently used data "
  "a better chance of staying in cache. Figure 2 suggests that reuse can help, although the benefit depends on how "
  "we access the data.")
p("We can test this by running the simple join and the radix join on the same inputs at several sizes. We should "
  "measure partitioning, building the hash tables, and probing for matches separately. Then we can compare total "
  "times to see where faster lookups save enough time to pay for partitioning.")
p("[1] P. Boncz, S. Manegold and M. L. Kersten. Database Architecture Optimized for the New Bottleneck: Memory "
  "Access. Proceedings of VLDB, 1999. Sections 2 and 3.", "cap")


def footer(canvas, doc):
    canvas.setFont("Helvetica", 8); canvas.setFillColor(SLATE)
    canvas.drawRightString(553, 24, str(doc.page))


args.pdf.parent.mkdir(parents=True, exist_ok=True)
doc = SimpleDocTemplate(str(args.pdf), pagesize=(595.28, 841.89), leftMargin=48, rightMargin=48,
                        topMargin=34, bottomMargin=40, title="Initial sequential scan experiment",
                        author="Radix hash join project")
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print(json.dumps({"report": str(args.pdf), "validation": validation,
                  "stride256_to_stride1_median_ratio": ratio}, indent=2))
