# Week 2 results

The baseline is a bucket-chained hash join. It keeps input records in their arrays and
stores array positions in bucket chains. Each record contains a 32-bit key and row ID.
The timed result counts matches and adds a row-pair checksum.

## Recorded timings

| Rows per input | Matches | Median build ms | Median probe ms | Median total ms | Estimated MiB |
|---:|---:|---:|---:|---:|---:|
| 10,000 | 5,000 | 0.017 | 0.023 | 0.040 | 0.48 |
| 100,000 | 50,000 | 0.133 | 1.008 | 1.141 | 4.29 |
| 500,000 | 250,000 | 0.704 | 7.311 | 8.079 | 19.44 |
| 1,000,000 | 500,000 | 2.822 | 19.228 | 21.915 | 38.89 |
| 2,000,000 | 1,000,000 | 6.532 | 47.254 | 54.042 | 77.78 |

The memory estimate includes the two input arrays, bucket array and chain array.
Allocator overhead and memory used by the rest of the process are additional.

## Reading the graphs

join_times.png compares median build, probe and total times. phase_times.png uses
mean phase times so the stacked bars add up to the mean total time.
At 2,000,000 rows per input, probing accounts for about 88.1%
of the mean join time. That gives us a phase to compare after radix partitioning.

A larger input also requires more insertions and lookups. We therefore need more
evidence before attributing growth in time to cache effects. Both the work count and
the memory footprint grow in this experiment. No hardware miss counters were collected.

## What comes next

Week 3 will partition both inputs and use this join routine on corresponding groups.
We will keep the input generator, hash function and count-only output policy consistent.
We will time partitioning separately and include it in the full radix join time.

## Run settings

```text
compiler=Apple LLVM 21.0.0 (clang-2100.1.1.101)
binary_bits=64
record_bytes=8
logical_cpu=-1
timer=CLOCK_UPTIME_RAW
seed=42
warmup_runs=1
repeats=7
thread_pinning=not supported on macOS; the thread uses the highest QoS class (prefers performance cores)
bucket_rule=at least 2 buckets per left row (2 * rows rounded up to a power of two)
```

The saved system.txt file identifies the macOS version and the CPU.
