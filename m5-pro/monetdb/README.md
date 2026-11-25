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
- **There was one until 2007, and its file until 2012.** Peter Boncz's
  radix module (`radix.mx`: `radix_cluster`, `phash_join`, `radix_join`,
  `radix_decluster`) was last compiled on 2007-09-12 (commit `33a91903a7`).
  On 2007-10-04 the build file commented it out (`#radix.mx`, commit
  `892ef9b796`, "Merged GDK-2 branch"); the file stayed in the source tree,
  not built, until it was deleted on 2012-03-28 (commit `0adbe4238f`). So
  Dec2011-SP2 still contains the file but does not build it. It never ran by
  itself: a person wrote the plan by hand and gave the bits of every pass,
  for example `radix_cluster(b, 6, 5)`.
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

## Experiment 2: Boncz's radix join from MonetDB, copied, on the tuner's data

Folder `exp2-boncz-radix/`. `src/boncz_radix.h` is a copy of Boncz's
`radix_cluster` and `phash_join` as MonetDB last compiled them (2007), on the
tuner's rows. `./fetch-original.sh` saves the original as
`original/radix-2007.mx` (not committed); the copy names the original line of
every function. The core functions are the same in the 2011 copy except for
variable names. Plain C: no MonetDB build needed.

How Boncz's join differs from the tuner's, on the same idea:

| | Tuner's join | Boncz's join (MonetDB 2007) |
|---|---|---|
| Key mixer | SplitMix64 (`mix`) | `(x>>7) ^ (x>>13) ^ (x>>21) ^ x` (`int_HUSSLE`, radix.mx:1270) |
| Cutting one pass | count every row, then move every row (2 reads) | give every piece an equal share, move rows until one is full, then estimate the rest from what was moved or count it, shift the pieces, finish (radix.mx:2053) |
| Finding the pieces when joining | list of piece bounds | walk both cut tables, find runs of equal piece number (radix.mx:2791) |
| Hash table | one per piece, boxes = rows / (rows per box), a knob | one shared, cleared per piece; boxes = the smallest power of 2 >= R's average piece size (0.5 to 1 row per box), not a knob |
| Box and link entries | 4 bytes | 8 bytes (`hash_t` = `oid`, 64-bit default in 2007) |
| Knobs | bits, passes, rows per box | bits per pass, given by hand |

What the copy changes, all listed at the top of `src/boncz_radix.h`: the
tuner's 8-byte rows and positions in rows, not bytes; each pass's output in
the tuner's prepared memory instead of a new table per pass; a match adds to
the checksum instead of being written out; no stopping at the first match
(radix.mx stopped when R's keys were known unique); `memmove` instead of an
overlapping `memcpy`.

| Step | What it checks | Result |
|---|---|---|
| 1 | Does the copy give right answers? 12 sizes, every setting | 486 of 486 right |
| 2 | One size (1,048,576 rows), same bits and passes as the tuner's pick, 5 repeats | Boncz's join 9.64 ns per row, the tuner's 10.14: Boncz 5% faster, in 5 of 5 repeats |
| 3 | 4 sizes, 10 shuffled rounds: tuner's join, Boncz's at the tuner's bits, Boncz's at the paper's formula | Boncz at the tuner's bits: 0.93x to 0.98x the tuner's time up to 8M rows, 1.03x at 16M. Boncz at the paper's formula: 1.14x to 1.34x, slower in 40 of 40 rounds |

### Step 1: right answers, nothing timed

```
cd exp2-boncz-radix && make step1 && ./bin/step1
```

12 table sizes from 1 to 262,144 rows (odd sizes too, so pieces are uneven),
every bits setting the tuner allows, 1 to 3 passes, with and without stopping
at the first match: 486 of 486 give the tuner's expected matches and checksum
(and the tuner's nested-loop answer at up to 4,097 rows).

How the 34,420 cuts finished: 30,964 (90.0%) fitted the equal first shares;
3,456 (10.0%) counted the rest exactly, 6 of them after first estimating it.
The tuner's keys are consecutive numbers, and Boncz's mixer spreads
consecutive numbers almost evenly over the pieces, so his guess of equal
sizes is usually right on this data. Data with uneven keys would make him
count more often.

### Step 2: the same bits and passes, at 1,048,576 rows

```
cd exp2-boncz-radix && make step2 && ./bin/step2      # output: runs/step2_1048576.txt
```

Both joins at the tuner's pick for this size, 11 bits in 1 pass; the tuner's
join with its 1/8 row per box, Boncz's with his own rule (512 rows per piece
here, so 512 boxes: 1 row per box). One untimed warm-up each, then 5 repeats,
alternating; every answer checked.

| Repeat | Boncz's join | Tuner's join |
|---|---|---|
| 1 | 9.52 | 10.16 |
| 2 | 9.59 | 10.19 |
| 3 | 9.44 | 10.16 |
| 4 | 9.91 | 10.04 |
| 5 | 9.72 | 10.16 |
| average | 9.64 | 10.14 |

ns per row. Boncz's join took 0.95 times the tuner's time, faster in 5 of 5.

Why: both of Boncz's cuts in a join (R and S) fitted the equal first shares,
so each read its input once; the tuner's cut always reads its input twice
(count, then move). That comes from this data: the keys are consecutive
numbers, and his mixer puts exactly as many of them in every piece. With
uneven keys his cut would have to count as well. One size, one setting, 5
repeats: not yet a result.

### Step 3: three settings at 4 sizes, measured like the race

```
cd exp2-boncz-radix && ./run.sh                    # about 12 seconds on the Apple M5
python3 summarize.py runs/<folder>                  # writes summary.csv, prints the table
./run.sh --quick                                    # checks that everything works; not a result
```

The race's rules: the tuner's data, 1 untimed warm-up per setting, then 10
rounds, each running the three settings once in a newly shuffled order;
every answer checked. Settings per size, from the race's plan:
- **tuner**: the tuner's join at its pick;
- **boncz-tuner**: Boncz's join at the same bits and passes (his own rows-per-box rule);
- **boncz-paper**: Boncz's join at the paper's formula (bits from page 63, passes from page 60).

Run `runs/apple-m5_2026-11-26_050245_step3/`, 11.7 seconds. ns per row:

| Rows per table | Tuner's join | Boncz at the tuner's bits | Boncz ÷ tuner (rounds Boncz faster) | Boncz at the paper's formula | Boncz-paper ÷ tuner (rounds faster) |
|---|---|---|---|---|---|
| 1,048,576 | 11 bits, 1 pass: 9.35 (9.18 to 9.58) | 8.69 (8.49 to 8.97) | 0.93 (10 of 10) | 7 bits, 2 passes: 12.50 (12.35 to 13.04) | 1.34 (0 of 10) |
| 4,194,304 | 12 bits, 1 pass: 9.64 (9.50 to 9.88) | 9.23 (9.02 to 9.52) | 0.96 (10 of 10) | 9 bits, 2 passes: 11.31 (11.25 to 11.41) | 1.17 (0 of 10) |
| 8,388,608 | 12 bits, 1 pass: 10.19 (10.08 to 10.49) | 9.95 (9.59 to 10.94) | 0.98 (8 of 10) | 10 bits, 2 passes: 11.63 (11.45 to 11.93) | 1.14 (0 of 10) |
| 16,777,216 | 10 bits, 1 pass: 10.45 (10.34 to 10.61) | 10.78 (10.69 to 11.15) | 1.03 (0 of 10) | 11 bits, 2 passes: 12.34 (12.13 to 12.57) | 1.18 (0 of 10) |

- At the tuner's bits and passes, Boncz's 2007 join was faster than the
  tuner's join up to 8,388,608 rows and slower at 16,777,216. None of his
  cuts in the timed joins had to count: on this data every cut fitted his
  equal first shares, so each read its input once.
- At the paper's formula, Boncz's join was slower than the tuner's pick in
  40 of 40 rounds. In the race, the paper's second pass was the whole gap
  for the tuner's join; whether it is for Boncz's join, this run does not test.
- Why Boncz's join loses at 16M rows has not been tested. One difference at
  that size: his box and link entries are 8 bytes, the tuner's 4.
- The tuner's picks were found for the tuner's join, not for Boncz's. Step
  4 is to let the tuner search Boncz's own settings.
