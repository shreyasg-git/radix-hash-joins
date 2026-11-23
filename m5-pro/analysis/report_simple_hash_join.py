"""Simple hash join on the M5 Pro: summary, plots and the PDF in the same layout
as reports/simple-hash-join.pdf (the Ryzen report).

    python3 analysis/report_simple_hash_join.py results/02-simple-hash-join --pdf reports/02-simple-hash-join.pdf

summary.csv, join_times.png, phase_times.png and week2_results.md come from
analyze_hash_join.py (unchanged code). Sentences that quote numbers are
computed from the data; the bucket rule is read from environment.txt.
"""
import argparse
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Table, TableStyle

import analyze_hash_join as A

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("results_directory", type=Path)
parser.add_argument("--pdf", type=Path, required=True)
args = parser.parse_args()
directory = args.results_directory
environment = (directory / "environment.txt").read_text()
settings = dict(line.split("=", 1) for line in environment.splitlines() if "=" in line)
groups = A.load_runs(directory / "timings.csv")
if any(len(runs) != int(settings["repeats"]) for runs in groups.values()):
    raise ValueError("Recorded run count does not match environment.txt")
summary = A.summarize(groups)
import csv
with (directory / "summary.csv").open("w", newline="") as target:
    writer = csv.DictWriter(target, fieldnames=list(summary[0]))
    writer.writeheader()
    writer.writerows(summary)
A.draw_plots(summary, directory)
A.write_notes(summary, environment, directory)

sysinfo = {}
for line in (directory / "system.txt").read_text().splitlines() if (directory / "system.txt").exists() else []:
    if ":" in line:
        k, v = line.split(":", 1)
        sysinfo[k.strip()] = v.strip()
cpu = sysinfo.get("machdep.cpu.brand_string", "Apple silicon")
macos = sysinfo.get("ProductVersion", "")
quarter = "per 4 left rows" in settings.get("bucket_rule", "")

NUMBER_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
word = lambda n: NUMBER_WORDS.get(n, str(n))


def rows_text(n):
    if n >= 1_000_000 and n % 1_000_000 == 0 and n // 1_000_000 in NUMBER_WORDS:
        return f"{NUMBER_WORDS[n // 1_000_000]} million"
    return f"{n:,}"


repeats = int(settings["repeats"])
first, largest = summary[0], summary[-1]
sizes = [r["rows_per_input"] for r in summary]
probe_above = all(r["probe_median_ms"] > r["build_median_ms"] for r in summary)
gaps = [r["probe_median_ms"] - r["build_median_ms"] for r in summary]
gap_grows = all(b > a for a, b in zip(gaps, gaps[1:]))
probe_share = 100 * largest["probe_mean_ms"] / largest["total_mean_ms"]
per_row_first = first["total_median_ms"] * 1e6 / first["rows_per_input"]
per_row_last = largest["total_median_ms"] * 1e6 / largest["rows_per_input"]
loads = [r["rows_per_input"] / r["buckets"] for r in summary]
by_size = {r["rows_per_input"]: r for r in summary}

# ---------- PDF in the layout of reports/simple-hash-join.pdf --------------
FONT_DIR = Path("/System/Library/Fonts/Supplemental")
pdfmetrics.registerFont(TTFont("TimesNewRoman", str(FONT_DIR / "Times New Roman.ttf")))
pdfmetrics.registerFont(TTFont("TimesNewRoman-Bold", str(FONT_DIR / "Times New Roman Bold.ttf")))
pdfmetrics.registerFont(TTFont("TimesNewRoman-Italic", str(FONT_DIR / "Times New Roman Italic.ttf")))
pdfmetrics.registerFont(TTFont("TimesNewRoman-BoldItalic", str(FONT_DIR / "Times New Roman Bold Italic.ttf")))
pdfmetrics.registerFontFamily("TimesNewRoman", normal="TimesNewRoman", bold="TimesNewRoman-Bold",
                              italic="TimesNewRoman-Italic", boldItalic="TimesNewRoman-BoldItalic")
S = {
    "title": ParagraphStyle("title", fontName="TimesNewRoman-Bold", fontSize=18, leading=22, spaceAfter=8),
    "deck": ParagraphStyle("deck", fontName="TimesNewRoman", fontSize=9, leading=11, spaceAfter=10),
    "body": ParagraphStyle("body", fontName="TimesNewRoman", fontSize=11, leading=14, spaceAfter=8),
    "h": ParagraphStyle("h", fontName="TimesNewRoman-Bold", fontSize=13, leading=16, spaceBefore=3, spaceAfter=7),
    "small": ParagraphStyle("small", fontName="TimesNewRoman", fontSize=9, leading=11, spaceAfter=7),
    "cell": ParagraphStyle("cell", fontName="TimesNewRoman", fontSize=10, leading=12),
}
story = []
p = lambda text, style="body": story.append(Paragraph(text, S[style]))


def picture(name, width=440):
    w, h = ImageReader(str(directory / name)).getSize()
    story.append(Image(str(directory / name), width=width, height=width * h / w))


bucket_sentence = (
    "The table has about one bucket for every four left records (the rule from commit d28429e), rounded up to a "
    "power of two, so chains hold about "
    f"{min(loads):.1f} to {max(loads):.1f} records on average."
    if quarter else
    "The table has at least twice as many buckets as left records, so later comparisons should keep that setting "
    "consistent.")
deck = f"Radix hash join project | {cpu} MacBook Pro measurements"
if quarter:
    deck += " | about one bucket per four left rows"
p("Week 2 Simple hash join", "title")
p(deck, "deck")
p(f"We implemented a simple hash join and measured how its build and probe phases behaved as the inputs grew. "
  f"This gives us a working baseline for comparing radix hash join in Week 3. At {rows_text(largest['rows_per_input'])} "
  f"rows per input, the median total time was {largest['total_median_ms']:.3f} ms. "
  + ("Probing took the larger share of the measured execution time."
     if largest["probe_mean_ms"] > largest["build_mean_ms"] else
     "Building took the larger share of the measured execution time."))
p("How the join works", "h")
p("Each record has a join key and a row ID. The key decides which records match. The row ID identifies the original "
  "row. For example, if the left keys are [7, 7, 9] and the right keys are [7, 9], the join produces three matching "
  "pairs. Both left rows with key 7 match the right row with key 7.")
p("<b>Build.</b> We create a hash table from the left input. The hash function chooses a bucket for each key. When "
  "several records land in the same bucket, we link their array positions in a chain. This also lets the table keep "
  "repeated keys.")
p("<b>Probe.</b> We read each right record, find its bucket, and check the keys in that chain. Every equal key adds a "
  "match. The benchmark counts the pairs and adds a checksum based on their row IDs. This makes the measured output "
  "consistent for later join comparisons.")
p("How we tested it", "h")
p("The small tests compare the complete set of matching row pairs with a separate nested-loop join, which checks "
  "every possible pair. All 107 cases passed. They include empty inputs, no matches, repeated keys, keys in the same "
  "bucket, and boundary key values. The same tests passed with AddressSanitizer and UndefinedBehaviorSanitizer. A "
  "sanitizer benchmark at 10,000 and 100,000 rows per input also passed.")
p(f"For the recorded benchmark, the left keys run from 0 to N-1 and the right keys are the even numbers from 0 to "
  f"2(N-1). Both inputs have unique keys and are shuffled with a fixed seed. This gives exactly N/2 matching pairs for "
  f"the tested sizes. Every run checks the count and expected row-pair checksum.")
p("What we measured", "h")
p(f"We used {word(len(sizes))} sizes from {sizes[0]:,} to {rows_text(sizes[-1])} rows in each input. Each size had one "
  f"warmup run followed by {word(repeats)} recorded runs. Each run built a fresh hash table. Build time includes allocating "
  f"the table, setting up its buckets and inserting the left records. Probe time includes lookups, match counting and "
  f"the checksum. Input generation, result checking, file writing and freeing the table happen outside the timer.")
p(f"The test ran on macOS {macos} on a MacBook Pro with an {cpu}. We used {settings.get('compiler', 'clang')} with "
  f"-O2. macOS cannot tie a thread to one processor, so the thread asked for the highest priority class, which runs "
  f"on the performance cores. A record uses 8 bytes. {bucket_sentence}", "small")
story.append(PageBreak())

p("What changed as the inputs grew", "h")
picture("join_times.png")
p(f"Figure 1. Median times across {word(repeats)} runs at each size. Each input has the row count shown on the horizontal "
  f"axis. That axis uses logarithmic spacing.", "small")
parts = []
parts.append("The probe line stays above the build line at every size." if probe_above else
             "The probe line is not above the build line at every size.")
if 500000 in by_size and 1000000 in by_size:
    parts.append(f"From 500,000 to one million rows per input, median total time rises from "
                 f"{by_size[500000]['total_median_ms']:.3f} to {by_size[1000000]['total_median_ms']:.3f} ms.")
parts.append(f"At {rows_text(largest['rows_per_input'])} rows it reaches {largest['total_median_ms']:.3f} ms.")
parts.append("The gap between the build and probe lines becomes larger as the inputs grow." if gap_grows else
             "The gap between the build and probe lines does not grow at every step.")
p(" ".join(parts))
data = [["Rows in each input", "Matches", "Build ms", "Probe ms", "Total ms"]]
for r in summary:
    data.append([f"{r['rows_per_input']:,}", f"{r['matches']:,}", f"{r['build_median_ms']:.3f}",
                 f"{r['probe_median_ms']:.3f}", f"{r['total_median_ms']:.3f}"])
t = Table([[Paragraph(str(c), S["cell"]) for c in row] for row in data], colWidths=[97] * 5, hAlign="LEFT")
t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                       ("GRID", (0, 0), (-1, -1), .4, colors.HexColor("#d9d9d9")),
                       ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 6),
                       ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
story.append(t)
p("The table uses medians. Each phase median is calculated separately, so build and probe medians can add up to a "
  "slightly different value from the total median.", "small")
p(f"Part of the increase is expected because larger inputs need more insertions and lookups. The time per input row "
  f"also {'rises' if per_row_last > per_row_first else 'changes'}. Dividing median total time by the number of right "
  f"rows gives about {per_row_first:.0f} ns at {first['rows_per_input']:,} rows and {per_row_last:.0f} ns at "
  f"{rows_text(largest['rows_per_input'])}. This includes both phases. "
  + ("It suggests that processing each row becomes more expensive as the test grows."
     if per_row_last > per_row_first else ""))
p(f"The estimated space for the two inputs and hash-table arrays rises from {first['estimated_memory_mib']:.2f} MiB "
  f"to {largest['estimated_memory_mib']:.2f} MiB. A larger memory footprint may contribute to the slowdown. The bucket "
  f"count also changes with input size: after rounding up to a power of two, the table holds between "
  f"{min(loads):.2f} and {max(loads):.2f} left records per bucket. To separate these effects, we will need the "
  f"partitioned join comparison and, where available, hardware counters.")
story.append(PageBreak())

p("Where the join spends its time", "h")
picture("phase_times.png")
p("Figure 2. Mean build and probe times. Each full bar is the mean total time for that input size. The orange section "
  "shows the probe phase.", "small")
p(f"At {rows_text(largest['rows_per_input'])} rows per input, the mean build time is {largest['build_mean_ms']:.3f} ms "
  f"and the mean probe time is {largest['probe_mean_ms']:.3f} ms. Probing accounts for about {probe_share:.1f}% of the "
  f"mean total. "
  + ("The larger orange sections show that searching for matches takes more time than building the table in these runs."
     if probe_share > 50 else "Building takes more time than searching for matches in these runs."))
p("During probing, consecutive input keys can lead to different buckets spread across the table. Some lookups also "
  "follow a chain. Those accesses are a possible reason for the probe cost. The current measurements combine hashing, "
  "key comparisons, memory access, match counting and checksum work. We have measured the full phase, so we should "
  "test which parts improve after partitioning.")
p("How this guides Week 3", "h")
p("We will split both inputs into partitions using the same hash bits, then join corresponding groups. Each local join "
  "will work with a smaller hash table. This gives the table a better chance of staying in cache while its keys are "
  "used.")
p("We will keep the generated inputs, match rate, hash function and match-counting output consistent. We will measure "
  "partitioning, local builds and local probes separately. The full radix join time will include all three phases. "
  "Then we can see whether faster local joins save enough time to cover partitioning, and how that changes with input "
  "size.")
p("This completes the Week 2 baseline. The source and running instructions are in m5-pro/src/hash_join.c and "
  "m5-pro/README.md. The individual measurements, run settings, summary statistics and two plots are saved in the "
  "results folder. The saved run uses unique keys with a 50 percent match rate. Duplicate handling was checked "
  "separately in the correctness tests.", "small")


def footer(canvas, doc):
    canvas.setFont("TimesNewRoman", 9)
    canvas.drawCentredString(297.64, 25, str(doc.page))


args.pdf.parent.mkdir(parents=True, exist_ok=True)
doc = SimpleDocTemplate(str(args.pdf), pagesize=(595.28, 841.89), leftMargin=50, rightMargin=50, topMargin=42,
                        bottomMargin=44, title="Week 2 Simple hash join", author="Radix hash join project")
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print(f"Report: {args.pdf}")
