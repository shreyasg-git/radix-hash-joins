# Week 2 explained: building the simple hash join

## Reading map

Read the main explanation in order first. Use the questions to practise, then open the code listings when you want to follow the implementation.

- [1. Where this week fits](#part-1)
- [2. What is a join? Start with a small example](#part-2)
- [3. What is stored in one record?](#part-3)
- [4. Why not compare every row with every other row?](#part-4)
- [5. What is hashing, and what is a bucket?](#part-5)
- [6. The hash table has three main parts](#part-6)
- [7. Build phase, one row at a time](#part-7)
- [8. Probe phase, one right row at a time](#part-8)
- [9. What exact data did we generate?](#part-9)
- [10. What is timed?](#part-10)
- [11. How many runs, sizes and threads?](#part-11)
- [12. How do we know the algorithm is correct?](#part-12)
- [13. Read the saved results carefully](#part-13)
- [14. Why this is useful for the radix join](#part-14)
- [15. Simple running instructions](#part-15)
- [16. Questions you may be asked](#part-16)
- [17. Source-code appendix](#part-17)

### A short measurement reminder

A millisecond (ms) is one thousandth of a second; a nanosecond (ns) is one billionth. Elapsed wall time is the time between start and end timestamps, including any interruption during that interval. It is not just the time the CPU spent actively executing our instructions.

The mean is the average of the recorded times. The median is the middle after sorting, averaging the two middle values when the count is even. Standard deviation describes variation; min/max are the fastest/slowest samples. A trial is one repetition. An excluded warmup is preparation practice, not a saved trial. A speedup of 2 means half the reference time; speedup is reference time divided by candidate time.

Keep input generation, join work, validation and plot generation separate in your explanation. Which operations are inside the timer is defined by each week's code, not by the filename of the program. New measurements will vary; the figures describe the saved run.

<a id="part-1"></a>

## 1. Where this week fits

Week 1 showed that the way we access memory can affect time even when the number of useful byte reads stays fixed. This week implements the actual database operation that we will later improve: a **simple hash join**. This is our baseline, meaning the reference implementation against which we compare radix joins.

This is an in-memory C program, not a database server. It generates two arrays of records, joins them, checks the answer and saves timings. We do not read input tables from a database or disk during the measured join.

The program is **single-threaded**. The main thread builds one table and then probes it. It is pinned to the first allowed logical processor. There are no parallel build or probe workers. Running seven trials does not mean seven threads.

<a id="part-2"></a>

## 2. What is a join? Start with a small example

A join finds pairs of rows whose selected values satisfy a condition. Our condition is equality of the **key**. The key is the field we compare; it is not the row's position in an array.

```text
Left table R                  Right table S
row ID   key                  row ID   key
0        7                    0        7
1        7                    1        9
2        9                    2        7
```

The answer is the row-ID pairs `(0,0), (1,0), (2,1), (0,2), (1,2)`. There are five matching pairs. Two left rows with key 7 and two right rows with key 7 produce **2 × 2 = 4 pairs** for that key.

This is an **inner equality join**: rows with no equal key on the other side do not produce an output pair. Duplicate keys are allowed. We must count every matching pair, not just every distinct key.

The large benchmark counts those pairs and calculates a checksum rather than storing a potentially huge output array. Small correctness tests do store all pairs so they can compare exact answers.

<a id="part-3"></a>

## 3. What is stored in one record?

```c
typedef struct {
    uint32_t key;
    uint32_t row_id;
} Record;
```

`uint32_t` is an unsigned 32-bit integer: four bytes on this machine. There are two fields, so the measured `sizeof(Record)` is eight bytes. `row_id` identifies the record's original row before shuffling. If the record moves to another array position, its row ID moves with it and stays the same.

With N rows, each input array has N records. At N = 1,000,000, that is about 8 MB per input and 16 MB for both, before the hash-table arrays. Here “one million rows per input” means **two million input rows in total**.

`Record *left` is a pointer to records. `left[i]` means the record at index i; `left[i].key` selects its key. Address arithmetic scales by the element size: `left + 1` advances eight bytes here. This differs from Week 1, where `unsigned char *p + 1` advanced one byte.

<a id="part-4"></a>

## 4. Why not compare every row with every other row?

A nested-loop join checks each left row against every right row. With N rows on each side, it makes N × N key comparisons. At one million rows per input, that would be a trillion candidate comparisons.

A hash join organizes the left rows so that a right key only needs to search one selected bucket. With well-distributed hashes and reasonable table sizing, the expected work is roughly proportional to the input sizes plus the output size. This is an expected behavior, not a guarantee for every key distribution.

We keep the nested-loop version for small tests because its logic is straightforward and independent of the hash-table lookup. It is not the large benchmark's baseline. **Our baseline is the simple hash join.**

<a id="part-5"></a>

## 5. What is hashing, and what is a bucket?

A **hash function** turns a key into an integer used for grouping or lookup. Our `mix` function spreads key bits into a 64-bit value using shifts, XOR and unsigned multiplications.

A **bucket** is an entry in the hash table that tells us where to start looking. Different keys can choose the same bucket; this is a **hash collision**. A collision is not a match. We still compare the full original keys before reporting a pair.

The number of buckets is a power of two and at least twice the left row count. For example, N = 5 needs at least 10 buckets; the next power of two is 16. Bucket numbers are in 0..15.

```c
size_t bucket = mix(records[i].key) & (bucket_count - 1);
```

If `bucket_count` is 16, `bucket_count - 1` is 15, binary `1111`. Bitwise AND keeps the lowest four hash bits. This is equivalent to taking the remainder modulo 16 for this unsigned value. It does not mean the original key itself is used as the array index.

The **load factor**, left rows divided by buckets, is at most 0.5 in this sizing policy. Lower load can shorten bucket chains but requires more memory.

<a id="part-6"></a>

## 6. The hash table has three main parts

| Field | What it stores |
|---|---|
| `records` | A pointer to the original left records. Build does not duplicate their payload into a separate node per row. |
| `buckets` | One array index per bucket: the first left row in its chain, or `SIZE_MAX` for empty. |
| `next` | One next-row index per left row, linking records that chose the same bucket. |
| `bucket_count` | How many entries exist in `buckets`. |

`size_t` is the type used for sizes and array indices; it is eight bytes in these 64-bit Ubuntu runs. `SIZE_MAX` is the largest representable `size_t`, used as a sentinel meaning “no row.” It is not an address to dereference.

These chains use **array indices**, not separately allocated linked-list nodes. The logical chain may still jump between distant positions in the input and next arrays.

<a id="part-7"></a>

## 7. Build phase, one row at a time

First allocate the bucket and next arrays. Fill every bucket with `SIZE_MAX`, meaning empty. Then process each left row:

```c
size_t bucket = mix(records[i].key) & (bucket_count - 1);
table.next[i] = table.buckets[bucket];
table.buckets[bucket] = i;
```

The first line selects a bucket. The second remembers its previous first row as the new row's successor. The third installs the new row as the bucket's first row.

Suppose rows 0 and 2 happen to choose bucket 3. These are assumed bucket choices for illustration, not computed claims about `mix`.

```text
Initially: buckets[3] = EMPTY
Insert row 0: next[0] = EMPTY; buckets[3] = 0
Insert row 2: next[2] = 0;     buckets[3] = 2
Now bucket 3's chain is 2 → 0 → EMPTY
```

Rows need not have equal keys to share this chain. That is why probe checks actual keys.

Build reads the left input in array order, but writes bucket positions chosen by hashes. It also writes the next array. Sequential input reading does not make every memory access sequential.

<a id="part-8"></a>

## 8. Probe phase, one right row at a time

For every right record, compute the same hash-to-bucket rule, read the bucket head and walk its chain. At each row index, read the stored left key and compare it with the right key.

```c
for (size_t row = table->buckets[bucket];
     row != SIZE_MAX;
     row = table->next[row]) {
    if (table->records[row].key == right[i].key) {
        /* This is one matching pair. */
    }
}
```

`table->field` accesses a field through a pointer to a structure. `table.records` would access a field through a structure value. `==` compares values; it does not assign one value to another.

On a match, form the pair of original row IDs, increment the match count, and add a hash of the packed pair to the checksum. `((uint64_t)left_row << 32) | right_row` places the left ID in the upper 32 bits and the right ID in the lower 32 bits. Unsigned checksum addition can wrap modulo 2^64; the expected checksum uses the same rule.

The benchmark passes `pairs = NULL`, so it does not write a full output list. The tests provide a buffer and its capacity. Probe keeps following the chain after a match, which is necessary for duplicate left keys.

Probe reads right records in their current array order, but bucket and chain accesses depend on the keys. This is where a large table can create distant memory lookups. It is related to Week 1's memory-access idea, although it is not the same workload as a fixed-stride byte scan.

<a id="part-9"></a>

## 9. What exact data did we generate?

Before shuffling:

```text
Left:  (key=i,   row_id=i), for i = 0..N-1
Right: (key=2*i, row_id=i), for i = 0..N-1
```

For N = 6, left keys are 0,1,2,3,4,5; right keys are 0,2,4,6,8,10. Matches are `(left row 0,right row 0)`, `(2,1)` and `(4,2)`. Thus the generated benchmark has `ceil(N/2)` pairs: half for the even sizes we test.

Both arrays are shuffled using a fixed seed, 42. Shuffling changes physical record order, not keys or row IDs and therefore not the join answer. The ordinary benchmark inputs have unique keys within each side. Duplicate support is exercised separately in the correctness tests; do not claim the basic timing workload contains many duplicates.

The expected checksum is calculated from the known matching even left keys. It does not call the hash join to decide its expected answer. This provides a separate check for the generated inputs.

<a id="part-10"></a>

## 10. What is timed?

```text
Generate and shuffle inputs                 [outside]
Compute expected answer                    [outside]
start timestamp
    allocate/init hash arrays and build    [build]
built timestamp
    search every right row, count/checksum [probe]
finished timestamp
Check result, write CSV, release hash table [outside]
```

`build_ms = (built - start) / 1e6`; `probe_ms = (finished - built) / 1e6`; `total_ms = (finished - start) / 1e6`. One millisecond is a million nanoseconds. `CLOCK_MONOTONIC` measures elapsed time without using the wall-clock date as a stopwatch.

**Week 2 total excludes freeing the table. Week 3/4 full total includes cleanup.** Do not calculate a formal Week 3 or Week 4 speedup by dividing these older Week 2 totals by a newer total. The later experiments rerun their simple baseline under the later timing boundaries.

Caches are left active. We do not call `CLFLUSH` in this hash join benchmark. Each measured run rebuilds the table, but rebuilding does not imply all its accesses start cold.

<a id="part-11"></a>

## 11. How many runs, sizes and threads?

The saved run has five sizes: 10,000, 100,000, 500,000, 1,000,000 and 2,000,000 rows **in each input**. There is one excluded warmup at each size and seven recorded runs, giving **5 × 7 = 35 recorded joins**. A 5,000,000-row case is supported if requested, but it is not part of the saved default Week 2 dataset.

There is one application thread for all of these joins. Each run completes before the next begins. One warmup is not an extra thread. The thread is pinned to one logical processor. Pinning does not reserve that processor exclusively or prevent shared-resource interference.

<a id="part-12"></a>

## 12. How do we know the algorithm is correct?

There are 107 small cases. They include empty inputs, no matches, duplicates, all combinations matching, zero and maximum 32-bit keys, deliberately colliding buckets, and 100 pseudo-random duplicate-key cases.

For each case, the nested-loop reference produces exact row-ID pairs. The hash join also produces pairs into a test buffer. Both lists are sorted and compared by count and contents. Sorting is needed because two correct joins may emit the same pairs in different orders.

The large benchmark separately checks its expected count and checksum after every run. A checksum is a compact check and can theoretically collide; exact small-case comparisons provide stronger evidence for those cases. Neither is a proof of all possible inputs.

AddressSanitizer and UndefinedBehaviorSanitizer are separate diagnostic builds used to catch memory-access and undefined-behavior problems. Sanitized runs are not used as our performance results.

<a id="part-13"></a>

## 13. Read the saved results carefully

| Rows per input | Median build ms | Median probe ms | Median total ms |
|---:|---:|---:|---:|
| 10,000 | 0.038 | 0.097 | 0.138 |
| 100,000 | 0.618 | 3.904 | 4.621 |
| 500,000 | 5.575 | 28.295 | 33.948 |
| 1,000,000 | 15.440 | 72.130 | 87.570 |
| 2,000,000 | 55.384 | 172.134 | 225.787 |

Each column is summarized independently. The median build and median probe may come from different runs, so their sum need not equal median total exactly. The means do add when the same runs and timing decomposition are used.

`join_times.png` compares median phases and total. `phase_times.png` stacks mean phase times. At the largest saved size, probing accounts for about **74.5% of mean total**. That gives us a concrete phase to investigate; it is not a universal constant for hash joins.

Larger inputs take more time, but they also require more insertions and lookups. This experiment changes both work count and memory footprint. We cannot attribute all growth to cache misses from these timings alone. Week 2 does not collect hardware miss counters.

The estimated memory is the two input arrays plus buckets and next indices:

```text
2 × N × sizeof(Record) + (N + bucket_count) × sizeof(size_t)
```

This excludes allocator and runtime overhead. It is not a direct measurement of the process's maximum resident memory.

<a id="part-14"></a>

## 14. Why this is useful for the radix join

Now we have a tested join, a reusable key hash, known input patterns, a clear output policy, and separate build/probe measurements. Week 3 can change the data organization while preserving these choices.

Radix partitioning aims to replace one large hash table with smaller local tables. It might reduce build/probe costs, but it adds partitioning. The right question becomes: **is the entire join faster after adding that cost?**

<a id="part-15"></a>

## 15. Simple running instructions

From the repository folder in Ubuntu:

```bash
gcc -O2 -std=c11 -Wall -Wextra src/hash_join.c -o hash_join
./hash_join --test
./hash_join results/simple-hash-join-new
python3 analysis/analyze_hash_join.py results/simple-hash-join-new
```

The code listings below show the files as they were written, when they were
named `hash_join.c` and `analyze.py` in the Week 2 folder; they now live at
`src/hash_join.c` and `analysis/analyze_hash_join.py`.

For a quick two-size check, use `./hash_join results/simple-hash-join-quick 3 100000`. The arguments mean output directory, recorded repeats per size, and maximum rows per input. Use a new folder name because this program refuses to overwrite an existing result directory.

The C program creates raw timing and environment files. The Python program reads them and writes summaries, notes and plots. It cannot reconstruct the exact old measurements if you delete the raw CSV.

<a id="part-16"></a>

## 16. Questions you may be asked

**Which table is built?** Left/R. Right/S probes it.

**Why is the row ID needed?** To identify the original matching row after records have been shuffled, and to validate pairs.

**Are collisions a bug?** No. They are expected and handled by chains plus full-key comparison.

**What if a key occurs three times on the left and twice on the right?** That key contributes six matching pairs.

**Is it random memory access everywhere?** No. Inputs are traversed in array order; bucket and chain lookups can jump around.

**What is the biggest cost we observed?** Probe dominates the saved largest-size mean, about 74.5%, under these settings.

**Does `-O2` parallelize it?** No. It requests compiler optimization. This code still has one benchmark thread.

**What is the difference from Week 1?** Week 1 sums selected bytes to study an access pattern. Week 2 joins structured records using a hash table and measures build/probe work.

<a id="part-17"></a>

## 17. Source-code appendix

Below is a block-by-block reading guide followed by a numbered source copy. Read build and probe first; then the input generator, timer boundaries and correctness tests. The Python guide explains how the measured CSV becomes the figures.

<!-- generated source appendix -->

### C and command syntax reminders

- `#include` brings declarations/source into this translation unit before compilation. It is not a runtime call. `#ifdef` chooses code at compile time.
- `static` on these functions means they are local to the translation unit. It does not create a thread. A structure groups named fields; a typedef gives a type a convenient name.
- `*` declares a pointer or dereferences one, depending on context. `&variable` takes an address; `a & mask` is bitwise AND. `&&` is logical AND. `|` is bitwise OR; `||` is logical OR.
- `=` assigns; `==` compares. `++` increments. `>>` shifts right, `<<` shifts left, `^` is bitwise XOR. `%` is integer remainder. `condition ? a : b` chooses one of two expressions.
- `sizeof(T)` gives bytes of storage for a type. Multiplying a count by this size converts elements to allocated bytes. A pointer addition already scales by its pointed-to type.
- `NULL` means no pointer target. `malloc` allocates, `free` releases. `memset` fills bytes, `memcpy` copies them and `memcmp` compares them. These functions do not implement key equality automatically.
- `qsort` rearranges items using a comparison function. It is used for tests here, not the timed join. `fopen` opens a file, `fprintf` formats text into it and `fclose` closes it. `perror` describes an OS error; `exit` stops the program.
- `argc` counts command-line arguments including the executable name. `argv[1]` is the first supplied argument. `return 0` from main indicates success. An executable is compiled C; a `.csv` is recorded text data; a `.py` is Python analysis code.
- Python `dict` maps keys to values; a list stores ordered items. `csv.DictReader` maps each CSV row's column names to text fields. Numerical fields must be converted before arithmetic. `Path` describes a file location. `if __name__ == "__main__"` runs the CLI only when the module is launched directly.

### Code file: hash_join.c

This listing has 297 lines. The numbers at the left are reading aids, not part of the code to compile.

#### Function/block reading guide

**Lines 36–48: `allocate`.** Check count times element-size for size_t overflow before malloc. Request at least a one-byte allocation for an empty array, simplifying later empty-input cleanup. On failure print an error and stop. Return a generic void pointer, assigned to the appropriate typed pointer by the caller.

**Lines 49–56: `mix`.** Apply XOR-right-shifts and two fixed 64-bit unsigned multipliers, then return the mixed integer. Overflow of unsigned arithmetic is intentional modulo arithmetic. This spreads bits; it does not check whether keys match. The same hash is used consistently by build and probe.

**Lines 57–65: `now_ns`.** Ask CLOCK_MONOTONIC for a timespec, fail explicitly if the call fails, and convert seconds plus nanoseconds into one uint64_t. Subtracting two such readings gives elapsed nanoseconds. This is unrelated to the calendar date shown by the OS.

**Lines 66–80: `build`.** Choose a power-of-two bucket count at least twice the left row count. Allocate bucket-head and next-index arrays. Initialize heads to SIZE_MAX. For each left record, select a bucket, link the row to its old head, and install the row as the new head. Return the structure; it borrows the input record array rather than copying it.

**Lines 81–103: `probe`.** For each right record, select its bucket and traverse the left-row index chain until SIZE_MAX. Compare full keys at every candidate. On each equality form original row-ID pairs, optionally save them to a checked test buffer, increment matches, and add the mixed packed pair to the checksum. Continue the chain after a match to handle duplicates.

**Lines 104–108: `release`.** Free bucket and next arrays. Do not free records: the caller owns the original or partitioned record array. Week 2 calls this outside total timing; the later measure_join calls it before its full timer ends.

**Lines 109–121: `reference_join`.** Use two nested loops to compare every small left/right pair and save those with equal keys. Return their count. This intentionally simple independent reference is used for tests, not the large performance measurements.

**Lines 122–127: `compare_pairs`.** Order pairs first by left original row ID, then by right original row ID. Return negative, zero or positive as required by qsort. Sorting lets tests compare answers independently of emission order.

**Lines 128–146: `check_case`.** Allocate worst-case pair capacity left_count times right_count for a small case. Run the nested reference and simple hash join. Sort both outputs, compare count and exact bytes, stop on disagreement, then release all test allocations. This is correctness work outside the performance dataset.

**Lines 147–151: `random_next`.** Advance a caller-owned 64-bit pseudo-random state by a fixed increment and mix it. Passing the state by pointer lets the caller keep the updated sequence state. Input generation and trial-order generation can use independent seeds.

**Lines 152–180: `self_test`.** Run seven explicit baseline edge/collision cases and 100 generated duplicate-key cases. A deliberate collision case searches for three keys selecting the same small bucket. Every case compares exact pairs to the nested-loop result, yielding the printed 107-case count.

**Lines 181–189: `shuffle`.** Run a Fisher-Yates-style shuffle from the end of an array. Pick an index in the remaining prefix and swap complete records. Keys and row IDs travel together. Modulo reduction is sufficient here for deterministic experiment data, though not a claim of perfect statistical sampling.

**Lines 190–198: `generate`.** Fill left records with key=i and row_id=i; fill right records with key=2*i and row_id=i. Shuffle both using the supplied seed state. This gives unique keys per side and ceil(N/2) matches. It happens outside the join timer.

**Lines 199–219: `pin_cpu`.** Read the process/thread allowed-CPU mask, find its first set bit, construct a mask containing only that CPU and apply affinity. Return the logical CPU index for metadata. This controls placement of the calling thread; it does not create more threads or reserve a core.

**Lines 220–230: `parse_number`.** Use strtoull to parse decimal text, checking conversion errors, the first character, leftover characters and an allowed range. Reject malformed or out-of-range arguments before using them as allocation sizes or loop limits.

**Lines 231–297: `main`.** Handle --test, otherwise parse output/repeats/max rows. Create a fresh result directory, pin main and write metadata/CSV headers. For each size up to the maximum, allocate/generate input records and independently compute expected matches/checksum. Run one excluded warmup plus requested recorded builds/probes. Timestamp before build, after build and after probe; validate and save measured intervals. Release table outside Week 2 timing, then free inputs after that size. Flush/close result output with error checks.

**Before the functions:** feature macros/includes expose Linux affinity, clocks and standard C APIs. `Record` is input data, `Pair` is test output, `HashTable` owns bucket/next arrays but borrows records, and `JoinResult` holds a count plus checksum.

#### Important statements, line by line

| Line | Statement | What it does |
|---:|---|---|
| 74 | <code>table.next[i] = table.buckets[bucket];</code> | Link the inserted left row to the previous bucket head before replacing that head. |
| 75 | <code>table.buckets[bucket] = i;</code> | Install this row index as the first entry of the bucket chain. |
| 86 | <code>for (size_t row = table-&gt;buckets[bucket]; row != SIZE_MAX; row = table-&gt;next[row]) {</code> | Follow an index chain, potentially to a distant left row. Stop only at the empty sentinel. |
| 87 | <code>if (table-&gt;records[row].key == right[i].key) {</code> | A shared bucket is only a candidate; this equality is the actual join condition. |
| 89 | <code>if (pairs) {</code> | Tests save actual pairs; large benchmarks pass NULL and avoid materializing an output relation. |
| 209 | <code>if (sched_setaffinity(0, sizeof(selected), &amp;selected)) {</code> | Set the calling thread CPU mask on Linux. It changes placement, not algorithm parallelism. |
| 210 | <code>perror("sched_setaffinity");</code> | Set the calling thread CPU mask on Linux. It changes placement, not algorithm parallelism. |
| 273 | <code>uint64_t start = now_ns();</code> | Begin this function's timed interval. Look for its matching end timestamp and cleanup placement to understand scope. |
| 275 | <code>uint64_t built = now_ns();</code> | Mark build completion; the next interval is probe. In the extension, build workers have already been joined. |
| 277 | <code>uint64_t finished = now_ns();</code> | Week 2 total ends here, before validation, CSV writing and table release. |
| 283 | <code>if (run) {</code> | Run zero is the excluded warmup; later runs are recorded. A run is a repetition, not a thread. |

#### Complete numbered source

<details>
<summary>Open hash_join.c</summary>

```text
   1  #define _GNU_SOURCE
   2  #include <errno.h>
   3  #include <inttypes.h>
   4  #include <limits.h>
   5  #include <sched.h>
   6  #include <stdint.h>
   7  #include <stdio.h>
   8  #include <stdlib.h>
   9  #include <string.h>
  10  #include <sys/stat.h>
  11  #include <time.h>
  12  
  13  /* Each record is a join key and the row number in its original table. */
  14  typedef struct {
  15      uint32_t key;
  16      uint32_t row_id;
  17  } Record;
  18  
  19  typedef struct {
  20      uint32_t left_row;
  21      uint32_t right_row;
  22  } Pair;
  23  
  24  typedef struct {
  25      const Record *records;
  26      size_t *buckets;
  27      size_t *next;
  28      size_t bucket_count;
  29  } HashTable;
  30  
  31  typedef struct {
  32      uint64_t matches;
  33      uint64_t checksum;
  34  } JoinResult;
  35  
  36  static void *allocate(size_t count, size_t size) {
  37      if (size && count > SIZE_MAX / size) {
  38          fprintf(stderr, "Allocation size overflow\n");
  39          exit(EXIT_FAILURE);
  40      }
  41      void *memory = malloc(count ? count * size : 1);
  42      if (!memory) {
  43          perror("malloc");
  44          exit(EXIT_FAILURE);
  45      }
  46      return memory;
  47  }
  48  
  49  static uint64_t mix(uint64_t value) {
  50      value ^= value >> 30;
  51      value *= UINT64_C(0xbf58476d1ce4e5b9);
  52      value ^= value >> 27;
  53      value *= UINT64_C(0x94d049bb133111eb);
  54      return value ^ (value >> 31);
  55  }
  56  
  57  static uint64_t now_ns(void) {
  58      struct timespec time;
  59      if (clock_gettime(CLOCK_MONOTONIC, &time)) {
  60          perror("clock_gettime");
  61          exit(EXIT_FAILURE);
  62      }
  63      return (uint64_t)time.tv_sec * UINT64_C(1000000000) + time.tv_nsec;
  64  }
  65  
  66  static HashTable build(const Record *records, size_t count) {
  67      size_t bucket_count = 1;
  68      while (bucket_count < count * 2) bucket_count *= 2;
  69      HashTable table = {records, allocate(bucket_count, sizeof(size_t)),
  70                         allocate(count, sizeof(size_t)), bucket_count};
  71      for (size_t i = 0; i < bucket_count; ++i) table.buckets[i] = SIZE_MAX;
  72      for (size_t i = 0; i < count; ++i) {
  73          size_t bucket = mix(records[i].key) & (bucket_count - 1);
  74          table.next[i] = table.buckets[bucket];
  75          table.buckets[bucket] = i;
  76      }
  77      return table;
  78  }
  79  
  80  /* In benchmarks we count matches. Small correctness tests also save row pairs. */
  81  static JoinResult probe(const HashTable *table, const Record *right, size_t count,
  82                          Pair *pairs, size_t capacity) {
  83      JoinResult result = {0, 0};
  84      for (size_t i = 0; i < count; ++i) {
  85          size_t bucket = mix(right[i].key) & (table->bucket_count - 1);
  86          for (size_t row = table->buckets[bucket]; row != SIZE_MAX; row = table->next[row]) {
  87              if (table->records[row].key == right[i].key) {
  88                  Pair pair = {table->records[row].row_id, right[i].row_id};
  89                  if (pairs) {
  90                      if (result.matches >= capacity) {
  91                          fprintf(stderr, "Pair output buffer is too small\n");
  92                          exit(EXIT_FAILURE);
  93                      }
  94                      pairs[result.matches] = pair;
  95                  }
  96                  ++result.matches;
  97                  result.checksum += mix(((uint64_t)pair.left_row << 32) | pair.right_row);
  98              }
  99          }
 100      }
 101      return result;
 102  }
 103  
 104  static void release(HashTable *table) {
 105      free(table->buckets);
 106      free(table->next);
 107  }
 108  
 109  static size_t reference_join(const Record *left, size_t left_count,
 110                               const Record *right, size_t right_count, Pair *pairs) {
 111      size_t count = 0;
 112      for (size_t i = 0; i < left_count; ++i) {
 113          for (size_t j = 0; j < right_count; ++j) {
 114              if (left[i].key == right[j].key) {
 115                  pairs[count++] = (Pair){left[i].row_id, right[j].row_id};
 116              }
 117          }
 118      }
 119      return count;
 120  }
 121  
 122  static int compare_pairs(const void *first, const void *second) {
 123      const Pair *a = first, *b = second;
 124      if (a->left_row != b->left_row) return a->left_row < b->left_row ? -1 : 1;
 125      return (a->right_row > b->right_row) - (a->right_row < b->right_row);
 126  }
 127  
 128  static void check_case(const char *name, const Record *left, size_t left_count,
 129                         const Record *right, size_t right_count) {
 130      size_t capacity = left_count * right_count;
 131      Pair *expected = allocate(capacity, sizeof(Pair));
 132      Pair *actual = allocate(capacity, sizeof(Pair));
 133      size_t expected_count = reference_join(left, left_count, right, right_count, expected);
 134      HashTable table = build(left, left_count);
 135      JoinResult result = probe(&table, right, right_count, actual, capacity);
 136      qsort(expected, expected_count, sizeof(Pair), compare_pairs);
 137      qsort(actual, (size_t)result.matches, sizeof(Pair), compare_pairs);
 138      if (result.matches != expected_count || memcmp(actual, expected, expected_count * sizeof(Pair))) {
 139          fprintf(stderr, "FAIL: %s\n", name);
 140          exit(EXIT_FAILURE);
 141      }
 142      release(&table);
 143      free(expected);
 144      free(actual);
 145  }
 146  
 147  static uint64_t random_next(uint64_t *state) {
 148      *state += UINT64_C(0x9e3779b97f4a7c15);
 149      return mix(*state);
 150  }
 151  
 152  static void self_test(void) {
 153      Record left[] = {{7,0},{7,1},{9,2},{UINT32_MAX,3},{0,4}};
 154      Record right[] = {{7,0},{7,1},{0,2},{UINT32_MAX,3},{42,4}};
 155      Record other[] = {{22,0},{23,1}};
 156      check_case("duplicates and boundary keys", left, 5, right, 5);
 157      check_case("no matches", left, 5, other, 2);
 158      check_case("empty left", NULL, 0, right, 5);
 159      check_case("empty right", left, 5, NULL, 0);
 160      check_case("both empty", NULL, 0, NULL, 0);
 161      check_case("all combinations match", left, 2, right, 2);
 162      Record collisions[3];
 163      size_t found = 0;
 164      for (uint32_t key = 0; found < 3; ++key) {
 165          if ((mix(key) & 7) == 0) {
 166              collisions[found] = (Record){key, (uint32_t)found};
 167              ++found;
 168          }
 169      }
 170      check_case("different keys in the same bucket", collisions, 3, collisions, 3);
 171      uint64_t state = 17;
 172      for (size_t trial = 0; trial < 100; ++trial) {
 173          Record a[32], b[48];
 174          for (size_t i = 0; i < 32; ++i) a[i] = (Record){random_next(&state) % 11, (uint32_t)i};
 175          for (size_t i = 0; i < 48; ++i) b[i] = (Record){random_next(&state) % 11, (uint32_t)i};
 176          check_case("random duplicate keys", a, 32, b, 48);
 177      }
 178      puts("Passed 107 cases against the nested-loop reference, comparing every matching row pair.");
 179  }
 180  
 181  static void shuffle(Record *records, size_t count, uint64_t *state) {
 182      for (size_t i = count; i > 1; --i) {
 183          size_t j = random_next(state) % i;
 184          Record temporary = records[i - 1];
 185          records[i - 1] = records[j];
 186          records[j] = temporary;
 187      }
 188  }
 189  
 190  static void generate(Record *left, Record *right, size_t count, uint64_t seed) {
 191      for (size_t i = 0; i < count; ++i) {
 192          left[i] = (Record){(uint32_t)i, (uint32_t)i};
 193          right[i] = (Record){(uint32_t)(2 * i), (uint32_t)i};
 194      }
 195      shuffle(left, count, &seed);
 196      shuffle(right, count, &seed);
 197  }
 198  
 199  static int pin_cpu(void) {
 200      cpu_set_t available, selected;
 201      if (sched_getaffinity(0, sizeof(available), &available)) {
 202          perror("sched_getaffinity");
 203          exit(EXIT_FAILURE);
 204      }
 205      for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) {
 206          if (CPU_ISSET(cpu, &available)) {
 207              CPU_ZERO(&selected);
 208              CPU_SET(cpu, &selected);
 209              if (sched_setaffinity(0, sizeof(selected), &selected)) {
 210                  perror("sched_setaffinity");
 211                  exit(EXIT_FAILURE);
 212              }
 213              return cpu;
 214          }
 215      }
 216      fprintf(stderr, "No available CPU\n");
 217      exit(EXIT_FAILURE);
 218  }
 219  
 220  static size_t parse_number(const char *text, size_t minimum, size_t maximum) {
 221      char *end;
 222      errno = 0;
 223      unsigned long long number = strtoull(text, &end, 10);
 224      if (errno || text[0] < '0' || text[0] > '9' || *end || number < minimum || number > maximum) {
 225          fprintf(stderr, "Expected an integer between %zu and %zu: %s\n", minimum, maximum, text);
 226          exit(EXIT_FAILURE);
 227      }
 228      return (size_t)number;
 229  }
 230  
 231  int main(int argc, char **argv) {
 232      if (argc == 2 && !strcmp(argv[1], "--test")) {
 233          self_test();
 234          return 0;
 235      }
 236      if (argc < 2 || argc > 4) {
 237          fprintf(stderr, "Usage: %s --test\n       %s OUTPUT_DIRECTORY [REPEATS] [MAX_ROWS]\n", argv[0], argv[0]);
 238          return EXIT_FAILURE;
 239      }
 240      size_t repeats = argc > 2 ? parse_number(argv[2], 1, 100) : 7;
 241      size_t max_rows = argc > 3 ? parse_number(argv[3], 10000, 5000000) : 2000000;
 242      /* A fresh directory prevents accidental replacement of recorded results. */
 243      if (mkdir(argv[1], 0755)) {
 244          perror(argv[1]);
 245          return EXIT_FAILURE;
 246      }
 247      int cpu = pin_cpu();
 248      char path[4096];
 249      if (snprintf(path, sizeof(path), "%s/timings.csv", argv[1]) >= (int)sizeof(path)) {
 250          fprintf(stderr, "Output path is too long\n");
 251          return EXIT_FAILURE;
 252      }
 253      FILE *output = fopen(path, "w");
 254      if (!output) { perror(path); return EXIT_FAILURE; }
 255      snprintf(path, sizeof(path), "%s/environment.txt", argv[1]);
 256      FILE *environment = fopen(path, "w");
 257      if (!environment) { perror(path); return EXIT_FAILURE; }
 258      fprintf(environment, "compiler=GCC %s\nbinary_bits=%zu\nrecord_bytes=%zu\nlogical_cpu=%d\n"
 259              "timer=CLOCK_MONOTONIC\nseed=42\nwarmup_runs=1\nrepeats=%zu\n",
 260              __VERSION__, 8 * sizeof(void *), sizeof(Record), cpu, repeats);
 261      fclose(environment);
 262      fprintf(output, "rows_left,rows_right,run,seed,matches,checksum,buckets,estimated_bytes,build_ms,probe_ms,total_ms\n");
 263      const size_t sizes[] = {10000, 100000, 500000, 1000000, 2000000, 5000000};
 264      for (size_t size_index = 0; size_index < sizeof(sizes) / sizeof(sizes[0]); ++size_index) {
 265          size_t count = sizes[size_index];
 266          if (count > max_rows) break;
 267          Record *left = allocate(count, sizeof(Record));
 268          Record *right = allocate(count, sizeof(Record));
 269          generate(left, right, count, 42);
 270          JoinResult expected = {(count + 1) / 2, 0};
 271          for (size_t i = 0; i < count; i += 2) expected.checksum += mix(((uint64_t)i << 32) | (i / 2));
 272          for (size_t run = 0; run <= repeats; ++run) {
 273              uint64_t start = now_ns();
 274              HashTable table = build(left, count);
 275              uint64_t built = now_ns();
 276              JoinResult result = probe(&table, right, count, NULL, 0);
 277              uint64_t finished = now_ns();
 278              if (result.matches != expected.matches || result.checksum != expected.checksum) {
 279                  fprintf(stderr, "Generated-data result mismatch\n");
 280                  return EXIT_FAILURE;
 281              }
 282              size_t bytes = 2 * count * sizeof(Record) + (count + table.bucket_count) * sizeof(size_t);
 283              if (run) {
 284                  fprintf(output, "%zu,%zu,%zu,42,%" PRIu64 ",%" PRIu64 ",%zu,%zu,%.6f,%.6f,%.6f\n",
 285                          count, count, run, result.matches, result.checksum, table.bucket_count, bytes,
 286                          (built - start) / 1e6, (finished - built) / 1e6, (finished - start) / 1e6);
 287              }
 288              release(&table);
 289          }
 290          free(left);
 291          free(right);
 292          if (fflush(output)) { perror("Writing timings"); return EXIT_FAILURE; }
 293          printf("%zu rows per input: verified %" PRIu64 " matches; %zu recorded runs\n", count, expected.matches, repeats);
 294      }
 295      if (fclose(output)) { perror("Closing timings"); return EXIT_FAILURE; }
 296      return 0;
 297  }
```

</details>

### Code file: analyze.py

This listing has 175 lines. The numbers at the left are reading aids, not part of the code to compile.

#### Function/block reading guide

**Lines 20–41: `load_runs`.** Read Week 2 CSV rows as dictionaries, convert sizes/times, reject duplicate size/run identities, check equal input sizes and expected matches, and validate positive finite times and their phase sum. Group accepted rows by size. This reads existing measurements; it does not run a join.

**Lines 44–64: `summarize`.** For each group calculate means, medians, extrema and sample standard deviations for each phase. Week 2 also checks checksum consistency and converts estimated bytes to MiB. Week 3 additionally attaches workload/bit/pass metadata and divides the same-workload/size baseline median by each candidate median to compute speedup.

**Lines 67–96: `draw_plots`.** Construct the plotted series from summarized measurements. Week 2 draws median phase/total curves and mean stacked bars. Week 4 draws scaling, tuning, phase, workload and own-baseline thread-speedup plots. Axes, labels and choices are established explicitly in this function; it does not infer CPU cache events from elapsed times.

**Lines 99–150: `write_notes`.** Create the Week 2 Markdown result table from computed statistics. Derive the largest-size probe share using mean probe divided by mean total. Include the environment settings and caution that input count as well as footprint grows.

**Lines 153–171: `main`.** Parse the result directory, validate/group the recorded rows, summarize their statistics and write the derived summary/plots/notes. The Week 4 version also imports Week 3 shared analysis and summarizes threads separately. This command does not execute the C benchmark.

#### Important statements, line by line

| Line | Statement | What it does |
|---:|---|---|
| 16 | <code>matplotlib.use("Agg")</code> | Use a noninteractive rendering backend so plots can be saved without opening a GUI window. |
| 59 | <code>entry[f"{phase}_median_ms"] = statistics.median(values)</code> | Summarize repeated values. Separately computed component medians need not add to the total median. |

#### Complete numbered source

<details>
<summary>Open analyze.py</summary>

```text
   1  """Summarize timings and draw the Week 2 baseline plots.
   2  
   3  Usage: python3 analyze.py results
   4  Requires matplotlib. Reads timings.csv and environment.txt in the supplied folder.
   5  """
   6  
   7  import argparse
   8  import csv
   9  import math
  10  import statistics
  11  from collections import defaultdict
  12  from pathlib import Path
  13  
  14  import matplotlib
  15  
  16  matplotlib.use("Agg")
  17  import matplotlib.pyplot as plt
  18  
  19  
  20  def load_runs(path):
  21      groups = defaultdict(list)
  22      recorded = set()
  23      with path.open(newline="") as source:
  24          for row in csv.DictReader(source):
  25              count = int(row["rows_left"])
  26              run = int(row["run"])
  27              identity = count, run
  28              if identity in recorded:
  29                  raise ValueError(f"Duplicate run {identity}")
  30              recorded.add(identity)
  31              if int(row["rows_right"]) != count or int(row["matches"]) != (count + 1) // 2:
  32                  raise ValueError("Input size or expected match count is inconsistent")
  33              times = [float(row[column]) for column in ("build_ms", "probe_ms", "total_ms")]
  34              if not all(math.isfinite(time) and time > 0 for time in times):
  35                  raise ValueError("Invalid execution time")
  36              if abs(times[0] + times[1] - times[2]) > 0.000003:
  37                  raise ValueError("Build and probe times do not add up to total time")
  38              groups[count].append(row)
  39      if not groups:
  40          raise ValueError("No recorded runs")
  41      return groups
  42  
  43  
  44  def summarize(groups):
  45      summary = []
  46      for count, runs in sorted(groups.items()):
  47          if len({row["checksum"] for row in runs}) != 1:
  48              raise ValueError("Checksums changed between repeated runs")
  49          entry = {
  50              "rows_per_input": count,
  51              "repeats": len(runs),
  52              "matches": int(runs[0]["matches"]),
  53              "buckets": int(runs[0]["buckets"]),
  54              "estimated_memory_mib": int(runs[0]["estimated_bytes"]) / 2**20,
  55          }
  56          for phase in ("build", "probe", "total"):
  57              values = [float(row[f"{phase}_ms"]) for row in runs]
  58              entry[f"{phase}_mean_ms"] = statistics.mean(values)
  59              entry[f"{phase}_median_ms"] = statistics.median(values)
  60              entry[f"{phase}_min_ms"] = min(values)
  61              entry[f"{phase}_max_ms"] = max(values)
  62              entry[f"{phase}_sd_ms"] = statistics.stdev(values) if len(values) > 1 else 0.0
  63          summary.append(entry)
  64      return summary
  65  
  66  
  67  def draw_plots(summary, directory):
  68      plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
  69      sizes = [row["rows_per_input"] for row in summary]
  70      figure, axes = plt.subplots(figsize=(8, 4.6))
  71      for phase in ("build", "probe", "total"):
  72          axes.plot(sizes, [row[f"{phase}_median_ms"] for row in summary], "o-", label=phase.capitalize())
  73      axes.set_xscale("log")
  74      axes.set_xlabel("Rows in each input (logarithmic scale)")
  75      axes.set_ylabel("Median execution time (ms)")
  76      axes.set_title("Simple hash join on increasing input sizes")
  77      axes.grid(alpha=0.2)
  78      axes.legend()
  79      figure.tight_layout()
  80      figure.savefig(directory / "join_times.png", dpi=180)
  81      plt.close(figure)
  82  
  83      figure, axes = plt.subplots(figsize=(8, 4.6))
  84      for index, row in enumerate(summary):
  85          build = row["build_mean_ms"]
  86          probe = row["probe_mean_ms"]
  87          axes.bar(index, build, color="#3276a5", label="Build" if index == 0 else None)
  88          axes.bar(index, probe, bottom=build, color="#d18436", label="Probe" if index == 0 else None)
  89      axes.set_xticks(range(len(sizes)), [f"{count:,}" for count in sizes])
  90      axes.set_xlabel("Rows in each input")
  91      axes.set_ylabel("Mean execution time (ms)")
  92      axes.set_title("Time spent building and probing the hash table")
  93      axes.legend()
  94      figure.tight_layout()
  95      figure.savefig(directory / "phase_times.png", dpi=180)
  96      plt.close(figure)
  97  
  98  
  99  def write_notes(summary, environment, directory):
 100      largest = summary[-1]
 101      probe_share = 100 * largest["probe_mean_ms"] / largest["total_mean_ms"]
 102      lines = [
 103          "# Week 2 results",
 104          "",
 105          "The baseline is a bucket-chained hash join. It keeps input records in their arrays and",
 106          "stores array positions in bucket chains. Each record contains a 32-bit key and row ID.",
 107          "The timed result counts matches and adds a row-pair checksum.",
 108          "",
 109          "## Recorded timings",
 110          "",
 111          "| Rows per input | Matches | Median build ms | Median probe ms | Median total ms | Estimated MiB |",
 112          "|---:|---:|---:|---:|---:|---:|",
 113      ]
 114      for row in summary:
 115          lines.append(
 116              f"| {row['rows_per_input']:,} | {row['matches']:,} | "
 117              f"{row['build_median_ms']:.3f} | {row['probe_median_ms']:.3f} | "
 118              f"{row['total_median_ms']:.3f} | {row['estimated_memory_mib']:.2f} |"
 119          )
 120      lines.extend([
 121          "",
 122          "The memory estimate includes the two input arrays, bucket array and chain array.",
 123          "Allocator overhead and memory used by the rest of the process are additional.",
 124          "",
 125          "## Reading the graphs",
 126          "",
 127          "join_times.png compares median build, probe and total times. phase_times.png uses",
 128          "mean phase times so the stacked bars add up to the mean total time.",
 129          f"At {largest['rows_per_input']:,} rows per input, probing accounts for about {probe_share:.1f}%",
 130          "of the mean join time. That gives us a phase to compare after radix partitioning.",
 131          "",
 132          "A larger input also requires more insertions and lookups. We therefore need more",
 133          "evidence before attributing growth in time to cache effects. Both the work count and",
 134          "the memory footprint grow in this experiment. No hardware miss counters were collected.",
 135          "",
 136          "## What comes next",
 137          "",
 138          "Week 3 will partition both inputs and use this join routine on corresponding groups.",
 139          "We will keep the input generator, hash function and count-only output policy consistent.",
 140          "We will time partitioning separately and include it in the full radix join time.",
 141          "",
 142          "## Run settings",
 143          "",
 144          "```text",
 145          environment.strip(),
 146          "```",
 147          "",
 148          "The saved system.txt file identifies the Ubuntu kernel and CPU exposed to WSL.",
 149      ])
 150      (directory / "week2_results.md").write_text("\n".join(lines) + "\n")
 151  
 152  
 153  def main():
 154      parser = argparse.ArgumentParser(description=__doc__)
 155      parser.add_argument("results_directory", type=Path)
 156      args = parser.parse_args()
 157      directory = args.results_directory
 158      environment = (directory / "environment.txt").read_text()
 159      settings = dict(line.split("=", 1) for line in environment.splitlines() if "=" in line)
 160      groups = load_runs(directory / "timings.csv")
 161      if any(len(runs) != int(settings["repeats"]) for runs in groups.values()):
 162          raise ValueError("Recorded run count does not match environment.txt")
 163      summary = summarize(groups)
 164      with (directory / "summary.csv").open("w", newline="") as target:
 165          writer = csv.DictWriter(target, fieldnames=list(summary[0]))
 166          writer.writeheader()
 167          writer.writerows(summary)
 168      draw_plots(summary, directory)
 169      write_notes(summary, environment, directory)
 170      print(f"Checked {sum(len(runs) for runs in groups.values())} runs across {len(groups)} input sizes.")
 171      print(f"Plots, summary.csv and week2_results.md saved in {directory}")
 172  
 173  
 174  if __name__ == "__main__":
 175      main()
```

</details>
