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
