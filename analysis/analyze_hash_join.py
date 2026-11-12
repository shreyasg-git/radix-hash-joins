"""Summarize timings and draw the Week 2 baseline plots.

Usage: python3 analysis/analyze_hash_join.py results/simple-hash-join
Requires matplotlib. Reads timings.csv and environment.txt in the supplied folder.
"""

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def load_runs(path):
    groups = defaultdict(list)
    recorded = set()
    with path.open(newline="") as source:
        for row in csv.DictReader(source):
            count = int(row["rows_left"])
            run = int(row["run"])
            identity = count, run
            if identity in recorded:
                raise ValueError(f"Duplicate run {identity}")
            recorded.add(identity)
            if int(row["rows_right"]) != count or int(row["matches"]) != (count + 1) // 2:
                raise ValueError("Input size or expected match count is inconsistent")
            times = [float(row[column]) for column in ("build_ms", "probe_ms", "total_ms")]
            if not all(math.isfinite(time) and time > 0 for time in times):
                raise ValueError("Invalid execution time")
            if abs(times[0] + times[1] - times[2]) > 0.000003:
                raise ValueError("Build and probe times do not add up to total time")
            groups[count].append(row)
    if not groups:
        raise ValueError("No recorded runs")
    return groups


def summarize(groups):
    summary = []
    for count, runs in sorted(groups.items()):
        if len({row["checksum"] for row in runs}) != 1:
            raise ValueError("Checksums changed between repeated runs")
        entry = {
            "rows_per_input": count,
            "repeats": len(runs),
            "matches": int(runs[0]["matches"]),
            "buckets": int(runs[0]["buckets"]),
            "estimated_memory_mib": int(runs[0]["estimated_bytes"]) / 2**20,
        }
        for phase in ("build", "probe", "total"):
            values = [float(row[f"{phase}_ms"]) for row in runs]
            entry[f"{phase}_mean_ms"] = statistics.mean(values)
            entry[f"{phase}_median_ms"] = statistics.median(values)
            entry[f"{phase}_min_ms"] = min(values)
            entry[f"{phase}_max_ms"] = max(values)
            entry[f"{phase}_sd_ms"] = statistics.stdev(values) if len(values) > 1 else 0.0
        summary.append(entry)
    return summary


def draw_plots(summary, directory):
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    sizes = [row["rows_per_input"] for row in summary]
    figure, axes = plt.subplots(figsize=(8, 4.6))
    for phase in ("build", "probe", "total"):
        axes.plot(sizes, [row[f"{phase}_median_ms"] for row in summary], "o-", label=phase.capitalize())
    axes.set_xscale("log")
    axes.set_xlabel("Rows in each input (logarithmic scale)")
    axes.set_ylabel("Median execution time (ms)")
    axes.set_title("Simple hash join on increasing input sizes")
    axes.grid(alpha=0.2)
    axes.legend()
    figure.tight_layout()
    figure.savefig(directory / "join_times.png", dpi=180)
    plt.close(figure)

    figure, axes = plt.subplots(figsize=(8, 4.6))
    for index, row in enumerate(summary):
        build = row["build_mean_ms"]
        probe = row["probe_mean_ms"]
        axes.bar(index, build, color="#3276a5", label="Build" if index == 0 else None)
        axes.bar(index, probe, bottom=build, color="#d18436", label="Probe" if index == 0 else None)
    axes.set_xticks(range(len(sizes)), [f"{count:,}" for count in sizes])
    axes.set_xlabel("Rows in each input")
    axes.set_ylabel("Mean execution time (ms)")
    axes.set_title("Time spent building and probing the hash table")
    axes.legend()
    figure.tight_layout()
    figure.savefig(directory / "phase_times.png", dpi=180)
    plt.close(figure)


def write_notes(summary, environment, directory):
    largest = summary[-1]
    probe_share = 100 * largest["probe_mean_ms"] / largest["total_mean_ms"]
    lines = [
        "# Week 2 results",
        "",
        "The baseline is a bucket-chained hash join. It keeps input records in their arrays and",
        "stores array positions in bucket chains. Each record contains a 32-bit key and row ID.",
        "The timed result counts matches and adds a row-pair checksum.",
        "",
        "## Recorded timings",
        "",
        "| Rows per input | Matches | Median build ms | Median probe ms | Median total ms | Estimated MiB |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        lines.append(
            f"| {row['rows_per_input']:,} | {row['matches']:,} | "
            f"{row['build_median_ms']:.3f} | {row['probe_median_ms']:.3f} | "
            f"{row['total_median_ms']:.3f} | {row['estimated_memory_mib']:.2f} |"
        )
    lines.extend([
        "",
        "The memory estimate includes the two input arrays, bucket array and chain array.",
        "Allocator overhead and memory used by the rest of the process are additional.",
        "",
        "## Reading the graphs",
        "",
        "join_times.png compares median build, probe and total times. phase_times.png uses",
        "mean phase times so the stacked bars add up to the mean total time.",
        f"At {largest['rows_per_input']:,} rows per input, probing accounts for about {probe_share:.1f}%",
        "of the mean join time. That gives us a phase to compare after radix partitioning.",
        "",
        "A larger input also requires more insertions and lookups. We therefore need more",
        "evidence before attributing growth in time to cache effects. Both the work count and",
        "the memory footprint grow in this experiment. No hardware miss counters were collected.",
        "",
        "## What comes next",
        "",
        "Week 3 will partition both inputs and use this join routine on corresponding groups.",
        "We will keep the input generator, hash function and count-only output policy consistent.",
        "We will time partitioning separately and include it in the full radix join time.",
        "",
        "## Run settings",
        "",
        "```text",
        environment.strip(),
        "```",
        "",
        "The saved system.txt file identifies the Ubuntu kernel and CPU exposed to WSL.",
    ])
    (directory / "week2_results.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_directory", type=Path)
    args = parser.parse_args()
    directory = args.results_directory
    environment = (directory / "environment.txt").read_text()
    settings = dict(line.split("=", 1) for line in environment.splitlines() if "=" in line)
    groups = load_runs(directory / "timings.csv")
    if any(len(runs) != int(settings["repeats"]) for runs in groups.values()):
        raise ValueError("Recorded run count does not match environment.txt")
    summary = summarize(groups)
    with (directory / "summary.csv").open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(summary[0]))
        writer.writeheader()
        writer.writerows(summary)
    draw_plots(summary, directory)
    write_notes(summary, environment, directory)
    print(f"Checked {sum(len(runs) for runs in groups.values())} runs across {len(groups)} input sizes.")
    print(f"Plots, summary.csv and week2_results.md saved in {directory}")


if __name__ == "__main__":
    main()
