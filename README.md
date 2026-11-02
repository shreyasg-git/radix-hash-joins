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