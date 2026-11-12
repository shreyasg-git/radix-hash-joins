# Future work

What this project could not do so far, why, and how to do it next.

## Limits of the machines

### Hardware miss counts
The paper explains its results with measured L1, L2 and TLB miss counts. We
have none. WSL2 (the Ryzen laptop) and macOS do not let a normal program read
the CPU's performance counters, so our cache explanations rest on timings and
data sizes only.

*Next:* boot into native Linux and run the radix partitioning benchmark after
`sudo sysctl kernel.perf_event_paranoid=1`. The program already reads cycles,
instructions, L1, last-level cache and data-TLB misses and writes them to
`timings.csv`. On macOS, Instruments (CPU Counters) can sample the same events
while the benchmark runs.

### One machine for all experiments
The stride scan and the simple hash join ran on the Ryzen laptop under WSL2;
radix partitioning runs on the MacBook. Absolute times cannot be compared
across machines.

*Next:* run every experiment on one machine. Two programs need porting first:
- The stride scan uses the x86 `CLFLUSH` instruction to empty the cache. On
  Apple silicon, evict the data by reading a buffer much larger than the
  caches, or use the ARM cache-maintenance instruction where the OS allows it.
- The simple hash join uses Linux-only CPU pinning.

### Page size and the TLB
The paper attributes much of the partitioning cost to TLB misses. Comparing
4 KiB pages with 2 MiB huge pages isolates that effect, but only Linux lets a
program choose huge pages. macOS uses 16 KiB pages and no choice.

*Next:* run the benchmark on native Linux with transparent huge pages, and with
explicitly reserved huge pages (`hugetlbfs`), together with TLB miss counts.

### Thread placement on macOS
macOS cannot pin a thread to a core. The benchmark asks for a performance core,
but the scheduler may still move it.

*Next:* repeat on Linux with the thread pinned, or log core usage with
`powermetrics` during the run.

## Parts of the paper not yet covered

### The full cost model
The paper predicts miss counts per cache level and per pass from data sizes and
cache parameters, and multiplies them by separately measured latencies (the
authors' later Calibrator tool measures these). Our report only checks the
paper's core assumption: a pass costs what its own fan-out costs.

*Next:*
- Measure load latency per cache level with a pointer-chasing benchmark.
- Implement the miss-count formulas, including the TLB term.
- Predict pass costs without using the measured curve.

### Narrow, decomposed storage
The paper argues for narrow records so each cache line carries only useful
bytes. Our records are always 8 bytes.

*Next:* add a payload of 8, 24 and 56 bytes and measure how partitioning cost
grows with record width.

## Scope of the experiment

### Data distributions
Keys are unique and uniformly hashed, so partitions are almost equal in size.

*Next:* use Zipf-distributed keys and many duplicates. Skew makes some
partitions far larger than others; measure that, and try splitting oversized
partitions.

### Modern partitioning techniques
Since 1999, partitioning has been improved with software write-combining
buffers (each partition first collects a cache line of records, then writes it
out at once) and non-temporal stores that bypass the cache. Both target the
same fan-out problem the paper solves with multiple passes.

*Next:* implement a write-combining scatter in `radix_pass` and see how far it
moves the point where one pass becomes slow.

### Measurement conditions
All runs were warm-cache runs inside one process, with one data seed, without
checking CPU frequency.

*Next:*
- Add cold-cache runs.
- Repeat whole runs as separate processes and with other seeds (`--seed`).
- Log CPU frequency and temperature, since laptops slow down when hot.

### Hash function
The benchmark uses one 64-bit mixing hash. The paper partitions on key bits
directly, and a cheaper hash would lower the computing cost.

*Next:* compare identity and multiplicative hashing, and store the hash in the
record so later passes do not recompute it.
