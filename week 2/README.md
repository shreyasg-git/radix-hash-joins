# Week 2: simple hash join

This week we built the simple hash join that will serve as our baseline for the
radix join. It builds a hash table from the left input, then searches it with
each record in the right input. We measured build and probe time separately
to see where the join spends its time.

## Saved results

The experiment uses one thread, pinned to one allowed logical processor.
We tested five sizes, from 10,000 to 2,000,000 rows in each input. Each size
has one excluded warmup and seven recorded runs, giving 35 recorded joins.
The generated inputs have unique keys and a 50% match rate. Separate tests
also check duplicate keys, empty inputs and hash collisions.

At the largest size, the median total was 225.787 ms. Probing accounted for
about 74.5% of the mean total time. That gives us a useful part of the join
to compare after adding radix partitioning.

- `results/report.pdf` explains the experiment, graphs and observations.
- `results/timings.csv` contains the individual measurements.
- `results/environment.txt` records the compiler and run settings.
- `results/system.txt` records the system and CPU information exposed to Ubuntu.

## Draw plots from the saved measurements

Open an Ubuntu or WSL terminal in this folder. With Python and Matplotlib
installed, run:

```bash
python3 analyze.py results
```

This creates `join_times.png`, `phase_times.png`, `summary.csv` and
`week2_results.md` inside `results`. It does not rerun the benchmark.

## Run the benchmark again

From this folder in Ubuntu, with GCC installed:

```bash
gcc -O2 -std=c11 -Wall -Wextra hash_join.c -o hash_join
./hash_join --test
./hash_join results-new
uname -sr > results-new/system.txt
lscpu >> results-new/system.txt
python3 analyze.py results-new
```

Use a fresh output folder name each time. For a quick check with three
recorded runs at 10,000 and 100,000 rows per input, use:

```bash
./hash_join results-quick 3 100000
python3 analyze.py results-quick
```

## Implementation and timing

Each record stores a 32-bit key and a 32-bit original row ID. Left keys are
0 to N-1; right keys are twice their original row IDs. Both arrays are
shuffled using seed 42. The hash table uses bucket chains stored as array
indices, with at least twice as many buckets as left records. Full keys
are compared to distinguish a real match from a hash collision.

Build time includes allocating and initializing the hash arrays and inserting
left records. Probe time includes searching, counting matching pairs and
calculating a row-pair checksum. Total time covers these two phases.
Input generation, result validation, CSV writing and freeing the hash table
are outside this week's timed interval. No explicit cache flush is used.
The benchmark counts matches rather than saving a large output table.

The `--test` command checks all matching row pairs against a nested-loop
reference in 107 small cases. Every recorded benchmark also checks its
expected count and checksum. The next step is to partition both inputs
and test whether smaller local joins save enough time to cover that work.
