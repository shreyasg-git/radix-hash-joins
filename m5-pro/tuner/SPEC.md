# Radix join tuner: frozen spec, version 1

Frozen on 2026-11-23. Version number in code: `SPEC_VERSION` in `src/tune.c`.
Every output file starts its rows with this number.

## What the tuner does, in three lines

1. It runs the radix hash join of Boncz, Manegold and Kersten (1999) on the
   machine it is on, with many different settings.
2. It times every run, and the time per row is the "loss": lower is better.
3. It keeps the settings with the lowest loss and writes down a rule for
   other table sizes.

It never reads the cache, TLB or memory sizes. The timings already contain
their effect, so the machine itself decides.

## What the tuner changes: three knobs

| Knob | What it means | Values tried |
|---|---|---|
| Radix bits B | Both tables are cut into 2^B pieces before joining. 0 means no cutting: one hash table on all of R. | 0 to min(18, log2(rows) - 4). The upper limit keeps about 16 or more rows per piece. |
| Passes P | How many rounds of cutting produce those 2^B pieces. | 1, 2, 3 (never more passes than bits) |
| Rows per bucket | How full each piece's hash table is on average. Fewer rows per bucket means more buckets. | 1/8, 1/4, 1/2, 1, 2, 4 |

## What is frozen: the same on every machine and every run

**The join**
- Cutting: each pass reads every row and writes it into the piece named by
  its hash bits. The bits are split over the passes as evenly as possible,
  and earlier passes get the extra bit. Pass 1 uses the highest group of the
  B low hash bits.
- Joining: for every piece, build a hash table on R's piece (a chain per
  bucket, the bucket taken from the hash bits above the B radix bits), then
  look up every row of S's piece in it.
- Bucket count: the smallest power of two that keeps the average at or below
  the chosen rows per bucket.
- Hash function: `mix`, the SplitMix64 finalizer (the hash of every
  experiment).
- Output: count of matching pairs plus a checksum of their row numbers. No
  result rows are written.

**The data**
- N rows per table. R's keys are 0 to N-1; S's keys are 0, 2, 4, ...,
  2(N-1). Both are shuffled with seed 42. Exactly half of S matches.
- A row is 8 bytes: a 4-byte key and a 4-byte row number.

**The loss**
- Loss = time of one whole join (cutting R, cutting S, building and
  looking up every piece) in nanoseconds, divided by N. Unit: ns per input row.
- All memory is allocated and written before the clock starts. Emptying the
  buckets is part of the time, because the join has to do it.
- One clock read before the join and one after. No clock reads inside.
- Every run's answer is checked against the known answer. A wrong answer
  stops the tuner.

**The machine setup**
- One thread. macOS: highest priority class (macOS cannot tie a thread to a
  core). Linux: tied to the core it starts on.

**The sizes**
- Training sizes: 2^20, 2^22 and 2^24 rows per table (1,048,576; 4,194,304;
  16,777,216).
- Held-out size: 2^23 (8,388,608). The rule is checked here, a size it was
  not built from.
- A size is skipped (and the skip is written down) if it needs more than a
  quarter of the machine's RAM. 2^24 needs about 1.4 GB.

**The search, for every training size**
1. Warm-up: 2 untimed joins.
2. Stage A, the loss curve: every (bits, passes) pair plus "no cutting", at
   1/2 row per bucket. 3 rounds. 52 settings at 2^24 rows.
3. Stage B, bucket load: the 4 (bits, passes) pairs with the lowest median
   from stage A, each with all 6 rows-per-bucket values. 3 rounds.
4. Stage C, final race: the 5 settings with the lowest median from stage B,
   plus "no cutting at 1/2 row per bucket" and stage A's winner, if not already
   among them. 11 rounds.
- A round runs every setting of the stage once, in a newly shuffled order
  (seeded), so a slow moment of the machine hits all settings alike.
- Winner: lowest median in stage C. Tied: a setting whose fastest run was
  faster than the winner's slowest run.
- Range edge: if the winner uses the largest bits value or the smallest or
  largest rows-per-bucket value, the tuner says so. A value outside the frozen
  range might be faster.

**The learned rule**
- Rows per piece T = the geometric mean of N / 2^B over the training sizes
  whose winner cut the tables (B > 0).
- Rule: B = round(log2(N / T)), limited to the allowed range. Passes and rows
  per bucket = the value that won most often (ties: the largest size's).
- Held-out check at 2^23: 1 untimed warm-up, then the rule's setting, the
  settings with 3 bits fewer to 3 bits more (same passes and rows per bucket),
  and "no cutting" at the rule's rows per bucket. 5 rounds. Report the rule's median divided by the best median, and whether
  they are tied.

## Not tuned in version 1, on purpose

Threads, the hash function, uneven bit splits over passes, row width, skewed
or duplicate keys in the timed data, huge memory pages, and the paper's later
improvements (for example, write buffers during cutting). Each would be a new
knob or a new frozen choice, so each needs a new spec version.

## Output files (frozen formats)

Written to `runs/<machine>_<date>_<time>/`:

| File | One row per | Columns |
|---|---|---|
| `trials.csv` | timed join | spec, rows_per_input, stage (A, B, C or T), round, position, bits, passes, rows_per_bucket, ns_per_row |
| `stages.csv` | setting in a stage | spec, rows_per_input, stage, bits, passes, rows_per_bucket, runs, median_ns_per_row, min_ns_per_row, max_ns_per_row |
| `best.csv` | training size, plus 2 held-out rows | spec, rows_per_input, role (train, test-best, test-rule), bits, passes, rows_per_bucket, median/min/max_ns_per_row, no_partitioning_ns_per_row, gain_vs_no_partitioning, tied_settings (bits/passes/load, ";"-separated), range_edge |
| `rule.txt` | key=value | spec, rows_per_partition, passes, rows_per_bucket, formula, test_rows, test_rule, test_best, test_rule_vs_best, test_tied, test_seconds |
| `machine.txt` | key=value | spec, profile, date, cpu, machine_tag, os, logical_cpus, ram_bytes, compiler, thread, cache_sizes ("not read"), skipped_sizes, seconds per size, seconds_total |
| `report.pdf` | | drawn by `report.py` from the files above |

## Profiles

- `full`: the official run, everything above.
- `quick` (`--quick`): sizes 2^16 and 2^18, held out 2^17, 1 round in stages A
  and B, 3 in C and in the held-out check. Only checks that everything runs.
  Its folder name ends in `_quick` and its numbers are not results.

## Changing the spec

Any change to a frozen item (a knob range, the data, the loss, the search,
the sizes, a file format) gets a new version number here and in
`SPEC_VERSION`. Results from different versions are never compared:
`report.py` refuses to put them on one page.
