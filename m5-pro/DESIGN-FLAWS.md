# Design flaws we found in the v2 programs, and how this folder fixes them

Found on 2026-11-22 while rerunning everything on the M5, fixed on 2026-11-23.
Both flaws are in programs that came from `../../radix-hash-joins-v2`. The
original v2 folder is unchanged. The fixes are only in this `m5-pro/` copy.

**Short version.**
1. The radix join timed itself with a stopwatch that it also clicked inside
   the race, so the clicks were counted as running time.
2. The threaded simple join made each core wait after every single insert,
   even when it was the only core working, so the one-thread time was 7 times
   too slow and every "speedup" was measured against that slow start.

Both old behaviours can still be switched on, so the difference can be shown
live: `--timing per-partition` (flaw 1) and `--insert cas-release` (flaw 2).
The results and reports from before the fixes are kept in
`archive/2026-11-22-before-fixes/`.

---

## Flaw 1: the radix join read the clock inside its own timing

### The idea in three lines
To time something you read a clock before and after. Reading the clock is not
free: on the M5 one read takes about 9 nanoseconds. If you read it thousands
of times *inside* the part you are timing, those reads become part of the time.

### What the program does
The radix join (experiment 4) first cuts both tables into pieces, then joins
piece 1 with piece 1, piece 2 with piece 2, and so on. For each piece it does
two steps: build a small hash table (build), then look up the other table's
rows in it (probe).

The report shows build time and probe time separately. To get them, v2 read
the clock after the build of every piece and after the probe of every piece:
2 reads per piece, all inside the timed join.

### Why that is a problem
The number of pieces is 2 to the power of the radix bits. With 14 bits there
are 16,384 pieces, so 32,768 clock reads land inside the measured time. At
about 9 ns each that is about 0.3 ms, while the whole join at 100,000 rows
takes about 1.4 ms.

So settings with many small pieces look slower than they really are. That
matters because the whole point of experiment 4 is to pick the best number of
bits: the flaw pushes the choice toward fewer bits.

### How we found it
The v2 report said the clock reads cost "under 1%". That was fixed text, not a
measurement. Two things were wrong:
- v2 measured one clock read as the middle value of single before-and-after
  pairs. The M5's clock only moves in steps of 41.7 ns, so almost every pair
  showed 0, and the report said a clock read costs "about 0 ns". The M5 copy
  now averages 1,000,000 reads instead (about 9 ns).
- With 9 ns per read, the share at 14 bits and 100,000 rows comes out at 18%
  on paper, not under 1%.

### The fix
- The build-and-probe loop over all pieces is now timed once, with no clock
  reads inside it. The total time is therefore clean.
- To still show build and probe separately, the program runs a second,
  build-only pass right after each run, timed on its own and not counted in
  the total. Build time = that pass; probe time = join time − build time.
- The same rule is used in experiment 6.
- Code: `src/radix_join.c`, function `radix_join`. The old way is still there
  behind `--timing per-partition`.

### What it changed
Measured the same evening, one straight after the other: the old way in
`results/04-radix-join-old-timing`, the clean way in `results/04-radix-join`.
One pass, median of 15 runs each.

| Rows per input | Radix bits | Pieces | Old way (ms) | Clean (ms) | Old way added |
|---|---|---|---|---|---|
| 100,000 | 8 | 256 | 1.145 | 1.133 | 1.0% |
| 100,000 | 12 | 4,096 | 1.234 | 1.194 | 3.3% |
| 100,000 | 14 | 16,384 | 1.525 | 1.382 | 10.4% |
| 1,000,000 | 12 | 4,096 | 12.360 | 12.182 | 1.5% |
| 1,000,000 | 16 | 65,536 | 14.169 | 13.724 | 3.2% |

- The more pieces, the bigger the error: up to 10.4% at 16,384 pieces.
- The 18% from the paper calculation did not show up in full. One likely
  reason: the processor can do part of a clock read while other work is still
  running. That is not proven.
- The conclusions of experiment 4 did not change. The best number of bits
  stayed the same at 4 of 7 sizes. At the other 3 it moved by 2 bits
  (100,000 rows: 6 to 8; 1,000,000: 10 to 12; 2,000,000: 12 to 10). At each
  of those 3, the old pick was within 2% of the new one and their runs
  overlapped, so they count as a tie either way.
- The radix join beat the simple join in every run from 1,000,000 rows up,
  before and after the fix.

---

## Flaw 2: the threaded simple join waited after every insert

### The idea in three lines
When several cores add rows to one shared hash table, two of them can hit the
same box at the same moment. Each insert must therefore be "all or nothing"
(atomic), or a row can be lost. Atomic is needed; waiting is not.

### What the program does
In experiment 5 the simple join's build phase lets every thread insert its
share of rows into one shared table. Each insert puts the new row at the
front of its box's chain: "the new row points to the old first row, and the
box now points to the new row".

### Why that was a problem
v2 did the insert with a compare-and-swap in "release" order. Release order
means: before this insert becomes visible, finish every memory read and write
this core started earlier. Each insert writes to a random box somewhere in a 128 MB array,
which is a slow trip to main memory. So every insert waited for the previous
slow write to finish. Normally a core keeps dozens of such trips in flight at
once; this rule allowed only one.

The waiting was not needed: no thread reads the table until every thread has
reached the barrier at the end of the build, and that barrier already makes
all writes visible to everyone.

This mattered most with one thread. Speedup is "time with 1 thread divided by
time with N threads". A one-thread time that is too slow makes every speedup
look better than it is.

### How we found it
The 1-thread simple join in experiment 5 took 568 ms to build its table, while
the plain 1-thread build in experiment 4 took 75 ms for the same 16,000,000
rows. Then we timed four ways of inserting, at 16,000,000 rows and 1 thread,
inside the experiment 5 program:

| Insert method | Build time, 1 thread |
|---|---|
| plain write, no atomic (experiment 4; only safe with 1 thread) | 76 ms |
| compare-and-swap, release order (v2) | 538 to 568 ms |
| compare-and-swap, no ordering | 99 to 113 ms |
| atomic swap, no ordering (the fix) | 80 to 88 ms |

### The fix
Each insert is now one atomic swap with no ordering request:
`old = swap(box, new row); next[new row] = old`. Two threads can still hit
the same box at the same moment, and both rows end up in the chain.
ThreadSanitizer (`make check`) checks all 5,524 test cases for threads that
touch the same memory without coordination and finds none.
Code: `src/parallel_join.c`, function `worker`. The old way is still there
behind `--insert cas-release`.

### What it changed
Measured the same evening, one straight after the other: the old way in
`results/05-parallel-join-old-insert`, the fix in `results/05-parallel-join`.
16,000,000 rows, 12 radix bits, median of 15 runs.

| Threads | Simple build, old (ms) | Simple build, fixed (ms) | Simple total, old | Simple total, fixed | Simple speedup, old | Simple speedup, fixed |
|---|---|---|---|---|---|---|
| 1 | 526 | 78 | 927 | 501 | 1.00x | 1.00x |
| 2 | 291 | 46 | 497 | 259 | 1.87x | 1.93x |
| 4 | 150 | 36 | 262 | 167 | 3.55x | 3.00x |
| 8 | 56 | 33 | 142 | 129 | 6.51x | 3.89x |

- The one-thread build went from 526 ms to 78 ms. For comparison, the plain
  build of experiment 4 (no atomics at all) took between 58 and 251 ms over
  its 15 runs, with a median of 105 ms. So the fixed atomic build now costs
  no more than the plain one.
- The simple join's speedup with 8 threads drops from 6.51x to 3.89x. The
  old number mostly measured how badly the waiting slowed down one thread.
- The radix join's lead shrinks. With 4 threads it was 4.0 times as fast as
  the simple join in the old run, and 2.3 times now. The old lead was too
  big because the simple join was held back. With 1 thread the lead is now
  2.0 times in this program and 2.5 times in experiment 4 (which probes each
  small table right after building it, while it is still in the cache).
- The radix join's code did not change.

**One more thing we saw: whole runs can shift.** The official experiment 5 run
came right after 10 minutes of experiment 4. A repeat 15 minutes later
(`results/05-parallel-join-repeat`) was 5% to 18% faster for both joins at
every thread count (median of 15 runs each). The ratios moved much less: radix over simple at 4 threads
was 2.3 in the official run and 2.35 in the repeat. Likely cause: heat or the
state of memory after a long run, not proven. So compare times within one
run; compare ratios across runs.

---

## Kept on purpose: Week 2 counted memory setup as build time

The Week 2 program (experiment 2, `src/hash_join.c`) asks the operating
system for the hash table's memory inside the build timer. The first write to
each 16 KiB memory page then makes the operating system set that page up,
which is also timed. More buckets means more pages, so part of the build-time
difference between the two bucket rules in reports 2 and 2b is memory setup,
not join work.

We kept it because report 2 reproduces the Week 2 report. Experiments 3 to 6
set up all memory before any timer starts, so experiment 6 measures the
bucket count without this effect.
