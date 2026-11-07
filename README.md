# Initial memory scan experiment

This experiment measures how the gap between memory reads affects scan time.
Each scan reads 200,000 bytes, one byte at a time. We compare scans after
flushing the selected cache lines with scans that immediately repeat the same reads.

## Plot the saved Ubuntu results

Open a terminal in the project folder. With NumPy and Matplotlib installed, run:

```bash
python3 analyze_and_report.py results-ubuntu --plots-only
```

Open the PNG files in `results-ubuntu/figures`.

## Run the benchmark again

On an x86-64 Ubuntu or WSL system with GCC, run:

```bash
gcc -O2 scan_benchmark.c -o scan_benchmark
./scan_benchmark results-new
python3 analyze_and_report.py results-new --plots-only
```

The benchmark runs 30 times per stride and method. Use a fresh folder name in
place of `results-new` to keep earlier runs.

`scan_benchmark.c` creates the three input files. `raw_timings.csv` holds the
measurements, `benchmark_metadata.json` holds the run settings, and
`timer_overhead.csv` holds the timer check. Keep them together in the results
folder so the Python script can read them.

## Week 2: building the simple hash join

In [Week 2](week%202/README.md), we moved from scanning bytes to joining two
tables. We implemented a simple hash join, checked it against a nested-loop
reference, and measured how much time it spent building the hash table and
searching it for matches.

The saved experiment uses one thread and five input sizes, from 10,000 to
2,000,000 rows in each table, with seven recorded runs per size. At the
largest size, probing took about 74.5% of the mean join time. This gives us
a baseline for checking whether radix partitioning can make the local
lookups faster while still paying for its extra preparation.

The folder contains the C implementation, plotting script, recorded timings,
run settings, system information and [Week 2 report](week%202/results/report.pdf).
Its README explains how to plot the saved results or run a new experiment.
