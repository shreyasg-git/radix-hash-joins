"""Experiment 2, step 3: summary of one run.

Reads RUN_DIR/trials.csv (written by bin/step3), writes RUN_DIR/summary.csv
and prints the same numbers: per size and setting, the average, median,
fastest, slowest and standard deviation in ns per row; the average divided
by the tuner's; in how many rounds the setting beat the tuner's join; and,
for Boncz's join, how many of its cuts had to count rows. Standard library only.

    python3 summarize.py runs/<folder>
"""
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def main(run_dir: Path) -> None:
    rows = list(csv.DictReader(open(run_dir / "trials.csv")))
    by = defaultdict(dict)  # (n, label) -> {round: row}
    order = []
    for r in rows:
        key = (int(r["rows_per_input"]), r["label"])
        if key not in by:
            order.append(key)
        by[key][int(r["round"])] = r
    out = []
    plan = [(int(r["rows_per_input"]), r["label"]) for r in csv.DictReader(open(run_dir / "plan.csv"))]
    for n, label in sorted(order, key=lambda k: plan.index(k) if k in plan else len(plan)):
        trials = by[(n, label)]
        tuner = by[(n, "tuner")]
        assert trials.keys() == tuner.keys(), f"rounds differ at {n} rows"
        rounds = sorted(trials)
        v = [float(trials[k]["ns_per_row"]) for k in rounds]
        t = [float(tuner[k]["ns_per_row"]) for k in rounds]
        first = trials[rounds[0]]
        counted = sum(int(trials[k]["boncz_cuts_counted"] or 0) for k in rounds) if label.startswith("boncz") else ""
        out.append({
            "rows_per_input": n, "label": label, "bits": first["bits"], "passes": first["passes"],
            "rows_per_bucket": first["rows_per_bucket"] or "boncz rule", "rounds": len(v),
            "mean": statistics.mean(v), "sd": statistics.stdev(v) if len(v) > 1 else 0.0,
            "median": statistics.median(v), "min": min(v), "max": max(v),
            "over_tuner": statistics.mean(v) / statistics.mean(t),
            "rounds_faster_than_tuner": "" if label == "tuner" else sum(1 for a, b in zip(v, t) if a < b),
            "boncz_cuts_counted": counted,
        })
    with open(run_dir / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        for o in out:
            w.writerow({k: (f"{x:.4f}" if isinstance(x, float) else x) for k, x in o.items()})
    print(f"{'rows':>10} {'setting':12} {'bits':>4} {'pass':>4} {'mean':>7} {'min':>7} {'max':>7} {'sd':>5} "
          f"{'/tuner':>7} {'faster':>7} {'counted':>7}")
    for o in out:
        faster = "" if o["label"] == "tuner" else f'{o["rounds_faster_than_tuner"]}/{o["rounds"]}'
        print(f'{o["rows_per_input"]:>10} {o["label"]:12} {o["bits"]:>4} {o["passes"]:>4} {o["mean"]:7.2f} '
              f'{o["min"]:7.2f} {o["max"]:7.2f} {o["sd"]:5.2f} {o["over_tuner"]:7.2f} {faster:>7} '
              f'{str(o["boncz_cuts_counted"]):>7}')


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 summarize.py RUN_DIR")
    main(Path(sys.argv[1]))
