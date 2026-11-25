# Paper's formula against the tuner

Times the radix join with the settings from the paper's formulas and with the
tuner's settings, on the same machine, 10 times each, and draws a report.

Everything that could change a timing is the tuner's own code:
`src/race.c` includes `../src/tune.c` (spec version 1), so the join, the
data, the clock, the answer check, the memory setup and the thread setup are
the same lines the tuner ran. Only the settings differ.

## Run it

```
./run.sh            # official run: 4 sizes, 10 rounds, about 30 seconds on the Apple M5
./run.sh --quick    # checks that everything works, in about 1 second; not a result
```

`TUNER_RUN=../runs/<folder>` picks the tuner run whose settings are used
(default: the newest full run). `NO_OPEN=1` keeps the PDF from opening.

Each run writes `runs/<machine>_<date>_<time>/`:
- `plan.txt`, `plan.csv`: every setting, and how it was worked out;
- `trials.csv`: every timed join; `summary.csv`: average, median, fastest,
  slowest and standard deviation of every setting;
- `machine.txt`: the machine and the run times;
- `report.pdf`: the answer, the charts and every number.

## What is compared, at 1,048,576, 4,194,304, 8,388,608 and 16,777,216 rows

| Setting | Bits | Passes | Rows per box |
|---|---|---|---|
| Tuner's pick | its winner at a size it learned from, else its rule | same | same |
| Paper's formula | formula 1 (page 63): smallest B with one piece of R plus its hash table, 12 bytes per row, in the first cache; the cache size comes from macOS | formula 2 (page 60): bits ÷ 6, rounded up (the paper's 64-entry page-address list) | the tuner's: the paper has no rule |
| Paper's bits, 1 pass | as above | 1: what formula 2 gives if this machine's list holds at least 2^bits entries | the tuner's |
| First race's version | formula 1 with 20 bytes per row (our table at 1/2 row per box) | formula 2 | 1/2 |
| No cutting | 0 | 0 | the tuner's |

Every setting gets 1 untimed warm-up join, then 10 timed joins, one per round.
Each round runs every setting of its size once, in a newly shuffled order.

## The paper's formulas, written out

Boncz, Manegold and Kersten, VLDB 1999. C is the number of rows in R.

1. Bits (page 63): B = log2(C × 12 / size of the first cache), rounded up, so
   that one piece of R plus its hash table (12 bytes per row) fits in the
   first cache. If the result is 1 or less, B = 0 (no cutting). The paper
   does not say how to round; rounding up is the choice that makes a piece fit.
   On the M5 the first cache is 131,072 bytes. At C = 16,777,216:
   16,777,216 × 12 / 131,072 = 1,536; log2(1,536) = 10.58; B = 11.
2. Passes (page 60): P = B / 6, rounded up. 6 = log2(64), because the paper's
   machine kept the addresses of 64 memory pages at a time, so one pass may
   cut into at most 64 pieces. At B = 11: 11 / 6 = 1.83; P = 2.
3. Time of the join (pages 60 and 62): T = C × wh + H × w′h + first-cache
   misses × lL2 + second-cache misses × lMem + page-address misses × lTLB.
   wh is the processor work per row (paper: 680 ns), H is the number of pieces
   (2^B), w′h is making and removing one piece's hash table (3,600 ns), and
   lL2, lMem, lTLB are the waits for the second cache (24 ns), main memory
   (412 ns) and a page-address lookup (228 ns). The cutting formula has the
   same shape, times the number of passes. The constants come from the
   processor's miss counters, which macOS does not let a program read, so this
   run does not use this formula.

`plan.py` (`fit_bits`, `paper_passes`) does formulas 1 and 2 for every size,
and each run's `plan.txt` shows the worked numbers.

## First result: Apple M5, 2026-11-24

`runs/apple-m5_2026-11-24_120145/`, 26 seconds.

| Rows per table | Tuner's pick | Paper's formula | Paper ÷ tuner | Paper's bits, 1 pass ÷ tuner |
|---|---|---|---|---|
| 1,048,576 | 11 bits, 1 pass, 1/8: 10.51 ns | 7 bits, 2 passes, 1/8: 15.82 ns | 1.51x | 1.09x |
| 4,194,304 | 12 bits, 1 pass, 1/8: 10.96 ns | 9 bits, 2 passes, 1/8: 15.23 ns | 1.39x | 1.04x |
| 8,388,608 | 12 bits, 1 pass, 1/8: 11.71 ns | 10 bits, 2 passes, 1/8: 15.33 ns | 1.31x | 1.03x |
| 16,777,216 | 10 bits, 1 pass, 1/8: 11.69 ns | 11 bits, 2 passes, 1/8: 14.91 ns | 1.27x | 1.02x |

- The tuner's pick was faster than the paper's formula in all 40 of 40 rounds.
- The whole gap is the passes rule: the same bits in 2 passes instead of 1
  took 1.25 to 1.37 times as long.
- The paper's bits in 1 pass tied with the tuner's pick at all 4 sizes (their
  fastest-to-slowest ranges overlap).

## Limits

- macOS does not report the number of entries in this machine's page-address
  list, so formula 2 uses the paper's 64.
- The tuner's picks come from timings on this machine with this data; the
  formula uses no timings.
- One machine, one thread, one data set (unique keys, both tables the same
  size, half of S matching).
