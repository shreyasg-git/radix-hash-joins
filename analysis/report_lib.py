"""Shared helpers for the experiment reports: reading results, statistics,
chart styling and a small PDF builder (ReportLab).

An analysis script reads one results folder, checks it, writes summary.csv
and figures/ into that folder, and builds a PDF in reports/.
Fonts (Source Serif Pro, Source Sans Pro, DejaVu Sans Mono; open licenses)
are bundled in analysis/fonts so reports look the same on every machine.
"""
import csv
import re
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (CondPageBreak, Image, KeepTogether, ListFlowable, ListItem, PageBreak,
                                Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
FONTS = Path(__file__).resolve().parent / "fonts"
PAPER = ("P. Boncz, S. Manegold and M. L. Kersten. Database Architecture Optimized for the "
         "New Bottleneck: Memory Access. Proceedings of VLDB, 1999.")

# Colors. Series: first slots of a colorblind-validated categorical palette, in
# fixed order. Text always stays in neutral ink.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#6250d6"]
INK, INK_2, INK_3 = "#1d1d1b", "#55544f", "#8a8984"
GRID, RULE, TINT, ACCENT = "#e6e5e1", "#d4d3ce", "#f2f5fa", "#2a78d6"

PAGE_W, PAGE_H = 595.28, 841.89
MARGIN_X = 56
TEXT_W = PAGE_W - 2 * MARGIN_X

# ---------- fonts ---------------------------------------------------------

_REGISTERED = False


def _register_fonts():
    global _REGISTERED
    if _REGISTERED:
        return
    faces = {
        "Serif": "SourceSerifPro-Regular.ttf", "Serif-It": "SourceSerifPro-It.ttf",
        "Serif-Semibold": "SourceSerifPro-Semibold.ttf", "Serif-Bold": "SourceSerifPro-Bold.ttf",
        "Sans": "SourceSansPro-Regular.ttf", "Sans-It": "SourceSansPro-It.ttf",
        "Sans-Semibold": "SourceSansPro-Semibold.ttf", "Sans-Bold": "SourceSansPro-Bold.ttf",
        "Mono": "DejaVuSansMono.ttf",
    }
    for name, file in faces.items():
        pdfmetrics.registerFont(TTFont(name, str(FONTS / file)))
    pdfmetrics.registerFontFamily("Serif", normal="Serif", bold="Serif-Semibold", italic="Serif-It",
                                  boldItalic="Serif-Semibold")
    pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Semibold", italic="Sans-It",
                                  boldItalic="Sans-Semibold")
    for file in ("SourceSansPro-Regular.ttf", "SourceSansPro-Semibold.ttf", "SourceSansPro-Bold.ttf",
                 "SourceSansPro-It.ttf"):
        font_manager.fontManager.addfont(str(FONTS / file))
    _REGISTERED = True


# ---------- data ----------------------------------------------------------

def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def read_environment(directory):
    env = {}
    for line in (Path(directory) / "environment.txt").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def cpu_model(directory):
    env_file = Path(directory) / "environment.txt"
    if env_file.exists():
        env = read_environment(directory)
        if env.get("cpu_model"):
            return env["cpu_model"]
    path = Path(directory) / "system.txt"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("Model name:") or line.startswith("machdep.cpu.brand_string:"):
                return re.sub(r"\(R\)|\(TM\)", "", line.split(":", 1)[1]).replace("  ", " ").strip()
    return "unknown CPU"


def pinning_text(env):
    """How threads were placed, for the Method sections."""
    if env.get("thread_pinning", "").startswith("not supported"):
        return ("macOS cannot pin threads to CPUs; threads ran at the highest priority class, which prefers the "
                "performance cores")
    return "one thread pinned to one CPU"


def bootstrap_ratio_ci(numerator, denominator, samples=4000, seed=1):
    """95% bootstrap confidence interval of median(numerator) / median(denominator)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    a, b = np.asarray(numerator, float), np.asarray(denominator, float)
    ra = np.median(rng.choice(a, (samples, a.size)), axis=1)
    rb = np.median(rng.choice(b, (samples, b.size)), axis=1)
    lo, hi = np.percentile(ra / rb, [2.5, 97.5])
    return float(lo), float(hi)


def steal_summary(rows, time_field):
    """VM interference: how many measurements lost CPU time to the hypervisor.
    Returns None when the results have no steal column or it was unavailable."""
    if not rows or "steal_ms" not in rows[0]:
        return None
    values = [(float(r["steal_ms"]), float(r[time_field])) for r in rows if r["steal_ms"] not in ("", "-1.0")]
    if not values:
        return None
    hit = [s for s, t in values if s > 0]
    big = [s for s, t in values if t > 0 and s > 0.01 * t]
    return {"measurements": len(values), "with_steal": len(hit), "over_1_percent": len(big),
            "total_ms": sum(hit)}


def steal_text(st):
    if st is None:
        return "Hypervisor steal time was not recorded."
    if st["with_steal"] == 0:
        return "No measurement lost CPU time to a hypervisor (steal time was zero throughout)."
    return (f"{st['with_steal']} of {st['measurements']} measurements lost some CPU time to the hypervisor "
            f"(steal time; {st['over_1_percent']} lost more than 1% of their duration, "
            f"{st['total_ms'] / 1000:.1f} s in total). Medians limit the effect of these runs.")


def is_virtual_machine(directory):
    path = Path(directory) / "system.txt"
    return path.exists() and "Hypervisor vendor" in path.read_text()


def summarize(values):
    values = sorted(values)
    n = len(values)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    q1, _, q3 = statistics.quantiles(values, n=4, method="inclusive") if n > 1 else (values[0],) * 3
    return {"n": n, "median": statistics.median(values), "mean": mean, "sd": sd,
            "cv": sd / mean if mean else 0.0, "min": values[0], "max": values[-1], "q1": q1, "q3": q3}


def write_summary(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def cache_bytes(env, level):
    """Cache size from environment.txt, e.g. cache_L2_Unified=2048K -> bytes."""
    for key, value in env.items():
        if key.startswith(f"cache_L{level}_") and "Instruction" not in key:
            number = float(value.rstrip("KMG"))
            return int(number * {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(value[-1], 1))
    return None


def machine_line(env):
    parts = [f"{env.get('online_cpus', '?')} logical CPUs"]
    for level in (1, 2, 3):
        size = cache_bytes(env, level)
        if size:
            parts.append(f"L{level} {human_bytes(size)}")
    return ", ".join(parts)


def human_bytes(n):
    for unit, size in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if n >= size:
            value = n / size
            return f"{value:.0f} {unit}" if value >= 10 or value == int(value) else f"{value:.1f} {unit}"
    return f"{n:.0f} B"


def counters_available(env):
    names = env.get("counters_available", "none")
    return [n for n in names.split(";") if n and n not in ("none", "page_faults")]


def fmt(x, digits=2):
    return f"{x:,.{digits}f}"


def pct(x, digits=0):
    return f"{100 * x:.{digits}f}%"


# ---------- charts ----------------------------------------------------------

FIG_W = TEXT_W / 72  # inches: figures are drawn at the size they appear in the PDF


def style_charts():
    _register_fonts()
    plt.rcParams.update({
        "font.family": "Source Sans Pro", "font.size": 8.5,
        "axes.edgecolor": INK_3, "axes.labelcolor": INK_2, "axes.labelsize": 8.5,
        "axes.spines.top": False, "axes.spines.right": False, "axes.linewidth": 0.7,
        "axes.grid": True, "axes.axisbelow": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.titlesize": 9, "axes.titleweight": "semibold", "axes.titlecolor": INK,
        "axes.titlelocation": "left", "axes.titlepad": 6,
        "xtick.color": INK_2, "ytick.color": INK_2, "xtick.labelsize": 8, "ytick.labelsize": 8,
        "xtick.major.size": 3, "ytick.major.size": 0, "xtick.major.width": 0.7,
        "legend.frameon": False, "legend.fontsize": 8, "legend.handlelength": 1.8,
        "figure.facecolor": "white", "axes.facecolor": "white",
        "lines.linewidth": 1.8, "lines.markersize": 4, "savefig.dpi": 240,
    })


def figure(height=2.7, ncols=1, sharey=False):
    return plt.subplots(1, ncols, figsize=(FIG_W, height), sharey=sharey)


def band(ax, xs, lows, highs, color):
    """Light band around a line, e.g. the interquartile range of the recorded runs."""
    ax.fill_between(xs, lows, highs, color=color, alpha=0.16, linewidth=0)


def save(fig, directory, name):
    folder = Path(directory) / "figures"
    folder.mkdir(exist_ok=True)
    fig.tight_layout(pad=0.4)
    fig.savefig(folder / name, bbox_inches="tight", pad_inches=0.03)
    plt.close(fig)
    return folder / name


# ---------- PDF -----------------------------------------------------------

_NUMERIC = re.compile(r"^[-+]?[\d,]*\.?\d+\s*(x|%|ms|ns|bits?)?$")


class Report:
    """Collects content, then writes one PDF.

    Layout: an eyebrow line, title and subtitle; a tinted summary box; numbered
    sections in a sans-serif face; body text in a serif face; tables and figure
    captions in the sans face.
    """

    def __init__(self, title, subtitle, eyebrow="Radix hash join project"):
        _register_fonts()
        S = ParagraphStyle
        self.styles = {
            "eyebrow": S("eyebrow", fontName="Sans-Semibold", fontSize=8, leading=10, textColor=colors.HexColor(ACCENT),
                         spaceAfter=6),
            "title": S("title", fontName="Sans-Bold", fontSize=21, leading=25, textColor=colors.HexColor(INK),
                       spaceAfter=5),
            "subtitle": S("subtitle", fontName="Sans", fontSize=10, leading=13, textColor=colors.HexColor(INK_2),
                          spaceAfter=14),
            "h1": S("h1", fontName="Sans-Semibold", fontSize=13.5, leading=17, textColor=colors.HexColor(INK),
                    spaceBefore=16, spaceAfter=6, keepWithNext=1),
            "h2": S("h2", fontName="Sans-Semibold", fontSize=10.5, leading=14, textColor=colors.HexColor(INK),
                    spaceBefore=8, spaceAfter=3, keepWithNext=1),
            "body": S("body", fontName="Serif", fontSize=10, leading=14.6, textColor=colors.HexColor(INK),
                      spaceAfter=7),
            "summary": S("summary", fontName="Serif", fontSize=10.5, leading=15.2, textColor=colors.HexColor(INK)),
            "bullet": S("bullet", fontName="Serif", fontSize=10, leading=14.2, textColor=colors.HexColor(INK)),
            "caption": S("caption", fontName="Sans", fontSize=8.5, leading=11, textColor=colors.HexColor(INK_2),
                         spaceBefore=3, spaceAfter=12),
            "note": S("note", fontName="Sans", fontSize=8.5, leading=11, textColor=colors.HexColor(INK_2),
                      spaceAfter=10),
            "cell": S("cell", fontName="Sans", fontSize=8.8, leading=11, textColor=colors.HexColor(INK)),
            "cellr": S("cellr", fontName="Sans", fontSize=8.8, leading=11, textColor=colors.HexColor(INK),
                       alignment=TA_RIGHT),
            "head": S("head", fontName="Sans-Semibold", fontSize=8.3, leading=10.5, textColor=colors.HexColor(INK_2)),
            "headr": S("headr", fontName="Sans-Semibold", fontSize=8.3, leading=10.5,
                       textColor=colors.HexColor(INK_2), alignment=TA_RIGHT),
            "code": S("code", fontName="Mono", fontSize=8, leading=11.2, textColor=colors.HexColor(INK)),
            "kpi": S("kpi", fontName="Sans-Semibold", fontSize=17, leading=20, textColor=colors.HexColor(INK)),
            "kpilabel": S("kpilabel", fontName="Sans", fontSize=8.3, leading=10.5, textColor=colors.HexColor(INK_2)),
        }
        self.title = title
        self.section = 0
        self.story = [Paragraph(eyebrow.upper(), self.styles["eyebrow"]),
                      Paragraph(title, self.styles["title"]),
                      Paragraph(subtitle, self.styles["subtitle"])]

    # -- text --
    def p(self, text, style="body"):
        self.story.append(Paragraph(text, self.styles[style]))

    def note(self, text):
        self.p(text, "note")

    def h(self, text, min_space=230):
        """Numbered section heading; starts a new page if less than min_space points are left."""
        self.section += 1
        self.story.append(CondPageBreak(min_space))
        self.p(f'<font color="{INK_3}">{self.section}</font>&nbsp;&nbsp;&nbsp;{text}', "h1")

    def h2(self, text):
        self.p(text, "h2")

    def bullets(self, items):
        flow = ListFlowable([ListItem(Paragraph(t, self.styles["bullet"]), leftIndent=13, value="•")
                             for t in items],
                            bulletType="bullet", start="•", leftIndent=13, bulletFontName="Sans",
                            bulletFontSize=9, bulletColor=colors.HexColor(INK_3), spaceBefore=0)
        self.story.append(flow)
        self.story.append(Spacer(1, 7))

    def summary(self, text):
        box = Table([[Paragraph(text, self.styles["summary"])]], colWidths=[TEXT_W])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(TINT)),
            ("LINEBEFORE", (0, 0), (0, -1), 2.5, colors.HexColor(ACCENT)),
            ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 10), ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
        ]))
        self.story += [box, Spacer(1, 12)]

    def key_numbers(self, items):
        """A row of 2-4 headline numbers: [(value, label), ...]."""
        cells = [[Paragraph(v, self.styles["kpi"]) for v, _ in items],
                 [Paragraph(l, self.styles["kpilabel"]) for _, l in items]]
        w = TEXT_W / len(items)
        t = Table(cells, colWidths=[w] * len(items), hAlign="LEFT")
        style = [("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                 ("TOPPADDING", (0, 0), (-1, 0), 8), ("BOTTOMPADDING", (0, 1), (-1, 1), 9),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("LINEABOVE", (0, 0), (-1, 0), 0.8, colors.HexColor(INK)),
                 ("LINEBELOW", (0, 1), (-1, 1), 0.5, colors.HexColor(RULE))]
        for i in range(1, len(items)):
            style.append(("LEFTPADDING", (i, 0), (i, -1), 10))
            style.append(("LINEBEFORE", (i, 0), (i, -1), 0.5, colors.HexColor(RULE)))
        t.setStyle(TableStyle(style))
        self.story += [t, Spacer(1, 14)]

    def code(self, text):
        safe = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace(" ", "&nbsp;").replace("\n", "<br/>"))
        box = Table([[Paragraph(safe, self.styles["code"])]], colWidths=[TEXT_W])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5f5f3")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(RULE)),
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        self.story += [KeepTogether([box]), Spacer(1, 10)]

    # -- tables and figures --
    def table(self, rows, widths=None, note=None, align=None, highlight_rows=()):
        """rows[0] is the header. Columns whose body cells all look numeric are
        right-aligned unless `align` (a string of 'l'/'r' per column) says otherwise."""
        ncol = len(rows[0])
        if align is None:
            align = ""
            for c in range(ncol):
                body = [str(r[c]).strip() for r in rows[1:] if str(r[c]).strip() not in ("", "-")]
                align += "r" if body and all(_NUMERIC.match(x) for x in body) else "l"
        if widths is None:
            widths = [TEXT_W / ncol] * ncol
        scale = TEXT_W / sum(widths) if sum(widths) > TEXT_W else 1
        widths = [w * scale for w in widths]
        cells = []
        for i, row in enumerate(rows):
            line = []
            for c, value in enumerate(row):
                key = ("head" if i == 0 else "cell") + ("r" if align[c] == "r" else "")
                line.append(Paragraph(str(value), self.styles[key]))
            cells.append(line)
        t = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
        style = [
            ("LINEABOVE", (0, 0), (-1, 0), 0.9, colors.HexColor(INK)),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor(INK_3)),
            ("LINEBELOW", (0, 1), (-1, -2), 0.3, colors.HexColor(GRID)),
            ("LINEBELOW", (0, -1), (-1, -1), 0.9, colors.HexColor(INK)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.6),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, 0), 5), ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ]
        for r in highlight_rows:
            style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor(TINT)))
        t.setStyle(TableStyle(style))
        block = [t]
        block.append(Paragraph(note, self.styles["caption"]) if note else Spacer(1, 12))
        self.story.append(KeepTogether(block))

    def figure(self, path, caption, width=TEXT_W):
        w, h = ImageReader(str(path)).getSize()
        caption = re.sub(r"^(Figure \d+\.)", r'<font name="Sans-Semibold" color="%s">\1</font>' % INK, caption)
        self.story.append(KeepTogether([Spacer(1, 4), Image(str(path), width=width, height=width * h / w),
                                        Paragraph(caption, self.styles["caption"])]))

    def page(self):
        self.story.append(PageBreak())

    def references(self, items):
        self.story.append(Spacer(1, 6))
        for i, text in enumerate(items, 1):
            self.note(f"[{i}] {text}")

    def build(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        def chrome(canvas, doc):
            canvas.saveState()
            canvas.setStrokeColor(colors.HexColor(RULE))
            canvas.setLineWidth(0.5)
            canvas.line(MARGIN_X, 40, PAGE_W - MARGIN_X, 40)
            canvas.setFont("Sans", 8)
            canvas.setFillColor(colors.HexColor(INK_3))
            canvas.drawString(MARGIN_X, 28, self.title)
            canvas.drawRightString(PAGE_W - MARGIN_X, 28, str(doc.page))
            canvas.restoreState()

        doc = SimpleDocTemplate(str(path), pagesize=(PAGE_W, PAGE_H), leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                                topMargin=52, bottomMargin=58, title=self.title,
                                author="Radix hash join project", invariant=1)
        doc.build(self.story, onFirstPage=chrome, onLaterPages=chrome)
        return path
