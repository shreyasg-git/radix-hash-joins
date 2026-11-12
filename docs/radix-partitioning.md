# Radix partitioning explained

Read this after `docs/simple-hash-join-explained.md`. It explains what the
radix partitioning benchmark measures, how the code works, and how to run it.

## 1. Why partition at all

The simple hash join builds one big table and probes it at random positions.
Once the table is larger than the caches, almost every probe waits for main
memory. A radix hash join first **partitions** both inputs by a few bits of the
key's hash, so partition *p* of R and partition *p* of S hold exactly the keys
that can match each other. Each pair can then be joined with a small hash table
that fits in cache.

Partitioning is not free, though. It reads and writes all the data, and it gets
slow when it writes to too many partitions at once. That cost is what this
benchmark measures.

## 2. One radix pass, step by step

```text
histogram: for every record, count[radix(record)]++
prefix sum: start[j] = count[0] + ... + count[j-1]
scatter:   for every record, out[start[radix(record)]++] = record
```

The histogram reads the input once and touches a small array of counters. The
scatter writes each record to one of 2^B places. When 2^B is larger than the
number of pages the TLB can translate quickly, or the number of cache lines the
CPU can keep open for writing, most of those writes miss.

## 3. Several passes

With 12 bits in 2 passes, pass 1 splits the input into 64 clusters on the top 6
bits, and pass 2 splits each of those 64 clusters into 64 on the next 6 bits.
The result is the same 4,096 partitions, but no pass ever writes to more than
64 places at once. This is the multi-pass radix-cluster of Boncz, Manegold and
Kersten. The price is reading and writing the data once more per extra pass.

## 4. The code

| File | What to read |
|---|---|
| `include/common.h` | `radix_of` (which bits), `split_bits` (bits per pass), `radix_pass` (histogram + scatter), `allocate_touched` (memory prepared before timing, optional huge pages), counters, shuffled run order |
| `src/radix_partition.c` | `partition()` runs all passes of one setting; `main()` loops over rounds and settings and writes `timings.csv` |
| `analysis/analyze_partition.py` | checks the CSV, computes medians, draws the figures, writes the PDF |
| `scripts/run_radix_partitioning.sh` | everything end to end |

The benchmark partitions 16,777,216 records (128 MiB) for every B from 0 to 20
with 1, 2 and 3 passes. On Linux it repeats everything with 4 KiB pages and
with 2 MiB huge pages. macOS does not let a program choose its page size, so
there only the default pages are used.

## 5. Measurement method

- All memory is allocated and written once before timing, so page faults are
  not timed.
- One warmup round, which is also verified, then 15 recorded rounds. Each round
  runs every setting once, in a newly shuffled order.
- Each pass is timed in two parts: histogram and scatter.
- The report uses medians, shows the middle half of the runs as bands, and
  calls two settings a tie when their run times overlap.
- Correctness: after the warmup every result is checked. Each record must sit
  in the partition its hash names, the partitions must cover all rows, and the
  records must be unchanged. `--test` also compares sorted output with the
  input for 238 small cases.
- Linux also records hypervisor steal time and, if the machine allows it,
  hardware counters (cycles, instructions, L1, last-level cache and TLB misses).

## 6. Run it

```bash
QUICK=1 scripts/run_radix_partitioning.sh   # 1-2 minute check
scripts/run_radix_partitioning.sh           # full run
```

## 7. Questions you may be asked

**Why does the histogram not get slower with more partitions?** It only
increments one of 2^B counters, and those counters stay in cache up to large B.
The scatter writes whole records to 2^B different places in memory.

**Why are settings run in a shuffled order?** So that no setting always runs
right after the same other setting; any cache or temperature effect left behind
by one setting spreads evenly over all of them.

**Why medians and not means?** A few runs can be slowed down by other work on
the machine. The median ignores them; the mean does not.

**What does the "predicted" line in the report mean?** It assumes each pass
costs what one pass with the same number of bits costs, and adds them up. That
is the paper's way of reasoning about passes, checked against the measurements.
