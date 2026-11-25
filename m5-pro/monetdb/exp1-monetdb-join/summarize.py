"""Experiment 1, step 4: summary of one run.

Reads RUN_DIR/trials.csv (written by bin/step4), writes RUN_DIR/summary.csv
and prints the same numbers. Only the Python standard library.

    python3 summarize.py runs/<folder>
"""
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def main(run_dir: Path) -> None:
    rows = list(csv.DictReader(open(run_dir / "trials.csv")))
    by = defaultdict(list)  # (rows_per_input, label) -> trial rows
    for r in rows:
        by[(int(r["rows_per_input"]), r["label"])].append(r)
    sizes = sorted({n for n, _ in by})
    out = []
    for n in sizes:
        tuner = {int(r["round"]): float(r["ns_per_row"]) for r in by[(n, "tuner")]}
        mdb = {int(r["round"]): r for r in by[(n, "monetdb")]}
        assert tuner.keys() == mdb.keys(), f"rounds differ at {n} rows"
        rounds = sorted(tuner)
        t = [tuner[k] for k in rounds]
        m = [float(mdb[k]["ns_per_row"]) for k in rounds]
        mj = [float(mdb[k]["join_ns_per_row"]) for k in rounds]
        mc = [float(mdb[k]["checksum_ns_per_row"]) for k in rounds]
        methods = sorted({mdb[k]["method"] for k in rounds})
        faster = sum(1 for k in rounds if tuner[k] < float(mdb[k]["ns_per_row"]))
        tr = by[(n, "tuner")][0]
        for label, v, extra in (
            ("tuner", t, f'{tr["bits"]} bits, {tr["passes"]} pass(es), {tr["rows_per_bucket"]} rows per box'),
            ("monetdb", m, "; ".join(methods)),
        ):
            out.append({
                "rows_per_input": n, "label": label, "setting": extra, "rounds": len(v),
                "mean": statistics.mean(v), "sd": statistics.stdev(v) if len(v) > 1 else 0.0,
                "median": statistics.median(v), "min": min(v), "max": max(v),
                "monetdb_join_mean": statistics.mean(mj) if label == "monetdb" else "",
                "monetdb_checksum_mean": statistics.mean(mc) if label == "monetdb" else "",
                "monetdb_over_tuner": statistics.mean(m) / statistics.mean(t) if label == "monetdb" else "",
                "rounds_tuner_faster": faster if label == "monetdb" else "",
            })
    with open(run_dir / "summary.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        for o in out:
            w.writerow({k: (f"{v:.4f}" if isinstance(v, float) else v) for k, v in o.items()})
    print(f"{'rows':>10} {'setting':8} {'mean':>7} {'min':>7} {'max':>7} {'sd':>6}  notes")
    for o in out:
        note = o["setting"]
        if o["label"] == "monetdb":
            note = (f'join {o["monetdb_join_mean"]:.2f} + checksum {o["monetdb_checksum_mean"]:.2f}; '
                    f'{o["monetdb_over_tuner"]:.2f}x the tuner; tuner faster in '
                    f'{o["rounds_tuner_faster"]} of {o["rounds"]} rounds; method: {o["setting"]}')
        print(f'{o["rows_per_input"]:>10} {o["label"]:8} {o["mean"]:7.2f} {o["min"]:7.2f} {o["max"]:7.2f} '
              f'{o["sd"]:6.2f}  {note}')


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 summarize.py RUN_DIR")
    main(Path(sys.argv[1]))
