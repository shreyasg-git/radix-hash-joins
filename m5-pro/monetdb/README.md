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

### Step 1: call MonetDB's join on 10 rows

```
cd exp1-monetdb-join && make step1 && ./bin/step1
```

Prints both tables, MonetDB's 5 matching pairs, and
`SAME ANSWER` (5 matches, checksum `21fe97ca324d8a4d` from both MonetDB and
the tuner's nested-loop join). Builds with 0 warnings.
