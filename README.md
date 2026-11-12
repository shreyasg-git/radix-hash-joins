# Radix hash join: memory access as the bottleneck

A scaled study of Boncz, Manegold and Kersten, *Database Architecture Optimized
for the New Bottleneck: Memory Access* (VLDB 1999).

## Experiments

| Concept | Program | Analysis | Results | Report |
|---|---|---|---|---|
| Stride scan: time vs. gap between reads | `src/scan_benchmark.c` | `analysis/analyze_scan.py` | `results/stride-scan/` | `reports/stride-scan.pdf` |
| Simple hash join (build + probe) | `src/hash_join.c` | `analysis/analyze_hash_join.py` | `results/simple-hash-join/` | `reports/simple-hash-join.pdf` |
| Radix partitioning: bits, passes, page size | `src/radix_partition.c` | `analysis/analyze_partition.py` | `results/radix-partitioning/` | `reports/radix-partitioning.pdf` |

## Folder structure

```text
src/        C programs
include/    common.h, shared code for the radix experiments
analysis/   Python scripts that plot results and build reports (fonts/ holds the report fonts)
results/    raw timings, settings and figures, one folder per experiment
reports/    PDF report per experiment
docs/       explanations of the code and the experiments; docs/future-work.md lists what is left
scripts/    end-to-end run scripts
Makefile    builds bin/, runs tests (make test) and sanitizer builds (make check)
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

## Radix partitioning

Measures what one radix partitioning pass costs per record as the number of
partitions grows (0 to 20 radix bits), whether splitting the bits over 2 or 3
passes helps, and, on Linux, whether 2 MiB huge pages change the picture. This
is the first half of the paper's radix hash join. See
`docs/radix-partitioning.md`.

Runs on macOS (Apple silicon) and Linux. On a Mac, install the command-line
tools once if `make` is missing (`xcode-select --install`), then:

```bash
QUICK=1 scripts/run_radix_partitioning.sh   # 1-2 minute check
scripts/run_radix_partitioning.sh           # full run: results/radix-partitioning, reports/radix-partitioning.pdf
```

The script builds the program, runs the correctness tests, runs the benchmark
(16,777,216 records, 1 verified warmup + 15 recorded rounds, settings in a new
shuffled order each round) and writes the report. It installs the Python
packages into a local `.venv` if they are missing and keeps a Mac awake while
it runs.

macOS does not let a program pin itself to a core, choose huge pages or read
hardware counters; the report notes this. On Linux the program also records
hypervisor steal time and, where allowed (`sudo sysctl
kernel.perf_event_paranoid=1`), cache and TLB miss counts.
