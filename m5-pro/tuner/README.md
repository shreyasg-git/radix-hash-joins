# Radix join tuner

Finds the fastest settings of the radix hash join on whatever machine it runs
on, by timing the join. It does not ask for, or read, the machine's cache
sizes: the timings already contain their effect.

This folder is standalone. It uses no file outside `tuner/`, and nothing
outside it uses this folder. Copy it to another machine and run it there.

## Run it

```
./tune.sh            # official run: about 2 minutes on the Apple M5
./tune.sh --quick    # checks that everything works, about 1 second; not a result
```

Needs a C compiler and `make`. The PDF also needs `python3` with
`matplotlib`; without them you still get the answer in the terminal and every
CSV file. `TUNE_NO_OPEN=1 ./tune.sh` keeps the PDF from opening.

Each run writes `runs/<machine>_<date>_<time>/`:
- `report.pdf`: the answer, the loss curves, the final race, the held-out check;
- `best.csv`, `rule.txt`: the answer as data;
- `trials.csv`: every timed join; `stages.csv`: every setting's median;
- `machine.txt`: what the machine was (recorded, never used by the search).

Compare machines (same spec version only):

```
python3 report.py runs/apple-m5_... runs/amd-ryzen-..._...
```

## What it tunes and what it keeps fixed

`SPEC.md` is the frozen contract (version 1). In short:
- **Tuned:** radix bits (how many pieces both tables are cut into), passes
  (in how many rounds they are cut) and rows per bucket (how full each piece's
  hash table is).
- **Fixed:** the join code, the data, the hash function, one thread, the
  timing rules, the loss (ns per input row), the search steps and the sizes.
- Changing anything fixed means a new spec version, and results of different
  versions are never compared.

## How the search works

The space is small enough to measure instead of guess:
1. **Stage A, the loss curve:** every number of bits with 1, 2 and 3 passes
   (52 settings at 16 million rows), 3 rounds each.
2. **Stage B:** the 4 best of those with all 6 bucket loads.
3. **Stage C, the final race:** the 5 best plus two references, 11 rounds each.
4. **The rule:** from the winners at 1, 4 and 16 million rows, the tuner
   learns how many rows one piece should hold, then checks that rule at 8
   million rows, a size it did not learn from.

## First result: Apple M5, 2026-11-23

`runs/apple-m5_2026-11-23_100003/`, 2 minutes 0 seconds in total.

| Rows per table | Fastest setting | ns per row | vs no cutting |
|---|---|---|---|
| 1,048,576 | 11 bits, 1 pass, 1/8 row per bucket | 9.92 | 1.72x as fast |
| 4,194,304 | 12 bits, 1 pass, 1/8 row per bucket | 10.27 | 2.58x as fast |
| 16,777,216 | 10 bits, 1 pass, 1/8 row per bucket | 11.00 | 2.68x as fast |

- The bottom of the loss curve is flat. At 16,777,216 rows, every value from
  9 to 12 bits came within 5% of the lowest point.
- The bucket load mattered more than the bits. Going from 1/2 to 1/8 row per
  bucket made the join 1.14 to 1.19 times as fast.
- 1/8 row per bucket is the smallest value spec 1 tries, so an even smaller
  value might be faster. Testing that needs spec version 2.
- Learned rule: aim for about 2,048 rows of R per piece. The held-out check at
  8,388,608 rows: the rule chose 12 bits, and the fastest was 11 bits. The
  rule's choice took 1.002 times as long, a tie.

## Against the paper's formula

`paper-race/` times the tuner's settings against the settings from the
paper's formulas, with the same join code, 10 rounds each. On the M5
(2026-11-24) the paper's formula took 1.27 to 1.51 times as long, all of it
from its rule of at most 6 bits per pass; the paper's bits in 1 pass tied with
the tuner. See `paper-race/README.md`.
