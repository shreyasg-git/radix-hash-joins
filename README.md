# Radix hash join: memory access as the bottleneck

A scaled study of Boncz, Manegold and Kersten, *Database Architecture Optimized
for the New Bottleneck: Memory Access* (VLDB 1999).

## Experiments

| Concept | Program | Analysis | Results | Report |
|---|---|---|---|---|
| Stride scan: time vs. gap between reads | `src/scan_benchmark.c` | `analysis/analyze_scan.py` | `results/stride-scan/` | `reports/stride-scan.pdf` |
| Simple hash join (build + probe) | `src/hash_join.c` | `analysis/analyze_hash_join.py` | `results/simple-hash-join/` | `reports/simple-hash-join.pdf` |

## Folder structure

```text
src/        C programs
analysis/   Python scripts that plot results and build reports
results/    raw timings, settings and figures, one folder per experiment
reports/    PDF report per experiment
docs/       explanations of the code and the experiments
```

## Stride scan

Measures how the gap between memory reads affects scan time. Each scan reads
200,000 bytes, one byte at a time, after flushing the cache lines or
immediately repeating the same reads. Needs x86-64 Linux or WSL with GCC.

```bash
gcc -O2 src/scan_benchmark.c -o scan_benchmark
./scan_benchmark results/stride-scan-new
python3 analysis/analyze_scan.py results/stride-scan-new --plots-only
```

The benchmark runs 30 times per stride and method. `raw_timings.csv` holds the
measurements, `benchmark_metadata.json` the settings and `timer_overhead.csv`
the timer check.

## Simple hash join

Builds one hash table on the left table and probes it with every row of the
right table, checked against a nested-loop reference. One thread, five input
sizes from 10,000 to 2,000,000 rows per table, seven recorded runs per size.
At the largest size, probing took about 74.5% of the mean join time. See
`docs/simple-hash-join.md` and `docs/simple-hash-join-explained.md`.

```bash
gcc -O2 -std=c11 -Wall -Wextra src/hash_join.c -o hash_join
./hash_join --test
./hash_join results/simple-hash-join-new
python3 analysis/analyze_hash_join.py results/simple-hash-join-new
```
