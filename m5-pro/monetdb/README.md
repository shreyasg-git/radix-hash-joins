# MonetDB's join, next to our tuner

MonetDB is a database built around the 1999 radix-join paper's ideas (two of
its authors, Boncz and Kersten, built it). These experiments run MonetDB's
own join code on the tuner's data, on the Apple M5, one small step at a time.

## Set up

```
./setup.sh      # downloads MonetDB at commit f17907b909 (2026-11-25) and builds it into install/
```

`source/`, `build/`, `install/` and `dbfarm/` are MonetDB's code, our build of
it and its databases. They stay out of this repository (`.gitignore`).

## What MonetDB does today, and what it did before

- **Today there is no radix join.** A join runs in MonetDB's kernel (`gdk/`):
  `BATjoin` (`gdk/gdk_join.c:4886`) picks merge join if both inputs are
  sorted, binary search if one is sorted and that is cheaper, and otherwise
  a hash join with one hash table on all of one input (`gdk/gdk_hash.c`:
  about rows × 8/7 boxes, plus one link per row). Nothing cuts the input into
  cache-sized pieces. For threads, the planner cuts the biggest table into
  row ranges (`monetdb5/optimizer/opt_mitosis.c`), one join per range.
- **There was one until 2012.** Peter Boncz's radix module
  (`monetdb5/modules/mal/radix.mx`, 4,957 lines: `radix_cluster`,
  `phash_join`, `radix_join`, `radix_decluster`) was deleted on 2012-03-28
  (commit `0adbe4238f`). The last release with it is Dec2011-SP2
  (2012-03-12). It never ran by itself: a person wrote the plan by hand and
  gave the bits of every pass, for example `radix_cluster(b, 6, 5)`.
- **Nothing in MonetDB tunes the join by timing it.** Every size in the join
  and the hash table is a fixed number in the code.

## Experiment 1: MonetDB's join on the tuner's data

Folder `exp1-monetdb-join/`. A small C program starts MonetDB's kernel in
memory (no server, no SQL), makes the tuner's tables with the tuner's own
code (`../tuner/src/tune.c`, included unchanged), calls `BATjoin`, and checks
the number of matches and the checksum against the tuner's answer.

| Step | What it checks | Result |
|---|---|---|
| 1 | Can our program call `BATjoin` at all? 10 rows. | Same 5 matches and checksum as the tuner's nested-loop join |
| 2 | One join at 1,048,576 rows: which method MonetDB picks, one timing | Hash join, hash table on R (as the tuner). 40.70 ns per row against the tuner's 9.95 (one run each) |
| 3 | Where MonetDB's time goes: building the hash table against looking up | Looking up is 68% at 1M rows and 73% at 16M rows; it slows down 2.25x from 1M to 16M rows |
| 4 | MonetDB against the tuner's pick: 4 sizes, 10 shuffled rounds, as in the race | Tuner faster in 40 of 40 rounds: MonetDB takes 4.48x the time at 1M rows and 7.31x to 7.88x from 4M to 16M rows |

### Step 1: call MonetDB's join on 10 rows

```
cd exp1-monetdb-join && make step1 && ./bin/step1
```

Prints both tables, MonetDB's 5 matching pairs, and
`SAME ANSWER` (5 matches, checksum `21fe97ca324d8a4d` from both MonetDB and
the tuner's nested-loop join). Builds with 0 warnings.

### Step 2: one join at 1,048,576 rows

```
cd exp1-monetdb-join && make step2 && ./bin/step2
```

First an untimed join with MonetDB's join messages on. They show, in order:
1. it checks whether either column is sorted (neither is; under 1 ms);
2. it samples 1,000 rows of each column to guess how many different keys
   there are (all 1,000 differ);
3. it picks `hashjoin` and builds a new hash table on R, the right input:
   the same side the tuner builds on;
4. the table has 1,198,372 boxes = 1,048,576 × 8/7, so 0.875 rows per box,
   plus one link per row; 685,625 boxes are in use.

Then new columns (so nothing from the first join carries over) and one timed
join of each kind. One run each, so this only shows that everything works:

| Join at 1,048,576 rows | ns per row |
|---|---|
| MonetDB `BATjoin` | 39.67 |
| turning its answer into the tuner's checksum | 1.03 |
| MonetDB total | 40.70 |
| tuner's join, its pick: 11 bits, 1 pass, 1/8 row per box | 9.95 |

Both answers: 524,288 matches, checksum `884915f6528a6bfd`.

One difference to keep in mind: MonetDB's join writes out every matching
pair (two lists of 524,288 row positions) and makes its hash table's memory
during the join; the tuner's join adds each pair to the checksum straight
away and reuses memory it set up before the clock starts.

### Step 3: building the hash table against looking up

```
cd exp1-monetdb-join && make step3 && ./bin/step3             # 1,048,576 rows, 5 repeats
./bin/step3 16777216 3                                         # 16,777,216 rows, 3 repeats
```

MonetDB's hash join does two jobs: build a hash table on R, then look up
every row of S in it. `BAThash` builds the same table on its own, so the
step times it, then times `BATjoin`, which then reports
"hashjoin using existing hash" (only looking up). For comparison, the whole
`BATjoin` on new columns reports "hashjoin using new hash; create hash".
Every repeat uses new columns; the two kinds alternate; every answer is
checked. Output: `runs/step3_1048576.txt`, `runs/step3_16777216.txt`.

| Average, ns per row | 1,048,576 rows (5 repeats) | 16,777,216 rows (3 repeats) | 16M ÷ 1M |
|---|---|---|---|
| build (`BAThash`) | 12.39 | 21.82 | 1.76 |
| look up (`BATjoin` with the table there) | 26.03 | 58.68 | 2.25 |
| build + look up | 38.42 | 80.50 | 2.10 |
| whole `BATjoin` | 43.68 | 82.97 | 1.90 |

- At 16,777,216 rows the hash table is about 211 MB (boxes 19,173,961 × 4
  bytes = 77 MB, links 16,777,216 × 4 bytes = 67 MB, keys 67 MB), against a
  16 MB second cache, so most lookups wait for main memory. This is the case
  the radix join was made for.
- The first whole join at 1M rows took 56.59 ns per row against 39.55 to
  41.04 for the other four: the program's first join pays one-time costs.
  Step 4 runs an untimed warm-up first, as the race does.
- For scale, the tuner's pick took 11.69 ns per row at 16,777,216 rows in the
  race (`../tuner/paper-race/`); MonetDB's whole join here is 7.1 times that.
  Step 4 measures both in the same program.

### Step 4: MonetDB against the tuner's pick, measured like the race

```
cd exp1-monetdb-join && ./run.sh                   # about 30 seconds on the Apple M5
python3 summarize.py runs/<folder>                  # writes summary.csv, prints the table
./run.sh --quick                                    # checks that everything works; not a result
```

The same rules as the race (`../tuner/paper-race/`): the tuner's own data and
code, every setting gets 1 untimed warm-up, then 10 rounds, each round runs
both settings once in a newly shuffled order, every answer is checked. The
tuner's picks are copied from the race's plan. MonetDB's time is its whole
`BATjoin` on new columns plus turning its answer into the tuner's checksum,
because the tuner's join makes the checksum inside its own timing. MonetDB
reported "hashjoin using new hash; create hash" in all 40 of its trials.

Run `runs/apple-m5_2026-11-26_044643_step4/`, 31.5 seconds. ns per row:

| Rows per table | Tuner's pick | Tuner: average (fastest to slowest) | MonetDB: average (fastest to slowest) | MonetDB ÷ tuner | Rounds the tuner was faster |
|---|---|---|---|---|---|
| 1,048,576 | 11 bits, 1 pass, 1/8 | 9.20 (9.01 to 9.53) | 41.19 (39.76 to 45.41) | 4.48 | 10 of 10 |
| 4,194,304 | 12 bits, 1 pass, 1/8 | 9.53 (9.32 to 9.84) | 69.63 (67.39 to 73.20) | 7.31 | 10 of 10 |
| 8,388,608 | 12 bits, 1 pass, 1/8 | 10.23 (10.02 to 10.59) | 80.59 (79.45 to 83.18) | 7.88 | 10 of 10 |
| 16,777,216 | 10 bits, 1 pass, 1/8 | 10.96 (10.30 to 15.57) | 86.15 (83.47 to 88.55) | 7.86 | 10 of 10 |

- MonetDB's checksum part is 1.07 to 2.40 ns per row of these; the rest is
  `BATjoin` (40.12 to 83.75).
- From 1M to 16M rows the tuner's join slows down 1.19x, MonetDB's 2.09x:
  the radix join keeps each piece's hash table in the cache, MonetDB's one
  table does not fit once it passes the 16 MB second cache.
- The tuner's pick ran faster here than in the race of 2026-11-24 with the
  same code and settings (9.20 against 10.51 ns per row at 1M rows, 10.96
  against 11.69 at 16M). Here it alternates only with MonetDB; in the race it
  alternated with 4 other settings. I have not tested why.

What this does not show yet:
- MonetDB makes the memory for its hash table and its two result lists
  inside the join; the tuner's join uses memory set up before the clock
  starts. How much of MonetDB's time that is has not been measured.
- MonetDB writes out every matching pair; the tuner's join does not keep
  them. A database needs the pairs to fetch other columns afterwards.
- One thread, one machine, one data set (unique keys, both tables the same
  size, half of S matching), as in the race.
