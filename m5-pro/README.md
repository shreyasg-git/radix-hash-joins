# Every experiment on one machine: Apple M5 MacBook Pro

This folder reruns all the experiments of the project on one laptop, a
MacBook Pro with an Apple M5 (4 performance cores, 6 efficiency cores,
32 GB, macOS). Nothing outside this folder was changed. The original
programs, results and reports in `../src`, `../results` and `../reports` stay
as they were (stride scan and simple hash join from the Ryzen laptop, radix
partitioning from this Mac).

## Run it

```bash
cd m5-pro
QUICK=1 scripts/run_experiments.sh   # a few minutes, results-quick/ (checks that everything works)
scripts/run_experiments.sh           # full run, results/ (about 18 minutes on the M5)
scripts/make_reports.sh              # reports/ from results/
make check                           # every test again under the memory and thread checkers
scripts/live.sh                      # live lab: type settings, get a report in seconds (see below)
```

`run_experiments.sh` builds everything, runs every correctness test, then
runs the experiments one after another and keeps the Mac awake. An
experiment whose folder has a `.done` marker is skipped, so the script can be
rerun after an interruption. Plug the laptop in and close other programs.

## What is in here

| # | Experiment | Program | Came from | Report |
|---|---|---|---|---|
| 1 | Stride scan | `src/scan_benchmark.c` | `../src/scan_benchmark.c`, adapted for Apple silicon | `reports/01-stride-scan.pdf` |
| 2 | Simple hash join | `src/hash_join.c` | `../src/hash_join.c`, adapted for macOS | `reports/02-simple-hash-join.pdf` |
| 2b | Same, Varun's bucket rule | `src/hash_join.c` built with `-DQUARTER_BUCKETS` | commit `d28429e` | `reports/02-simple-hash-join-quarter-buckets.pdf` |
| 3 | Radix partitioning | `src/radix_partition.c` | `../src/radix_partition.c`, unchanged | `reports/03-radix-partition.pdf` |
| 4 | Simple vs radix join | `src/radix_join.c` | `../../radix-hash-joins-v2`, timing fixed | `reports/04-radix-join.pdf` |
| 5 | Threads: 1, 2, 4 and 8 | `src/parallel_join.c` | `../../radix-hash-joins-v2`, insert fixed | `reports/05-parallel-join.pdf` |
| 6 | Bucket count and hash function | `src/bucket_sweep.c` | new in this folder | `reports/06-bucket-sweep.pdf` |
| 7 | Which conclusions still hold | `analysis/final_analysis.py` | `../../radix-hash-joins-v2` | `reports/07-final-analysis.pdf` |

`include/common.h` is the v2 version: it contains everything in
`../include/common.h` plus the hash table that experiments 4 and 5 share.
One function changed, see "Clock cost" below.

## What had to change for the Mac, and why

**Stride scan.** The original empties the cache with the x86 instruction
`CLFLUSH`. Its ARM counterpart (`DC CIVAC`) runs on macOS but does nothing
measurable for a normal program: random reads took 2.6 ns before and after
it. So before each "flushed" scan the program reads a separate 512 MiB
buffer, which takes 25 to 35 ns per read afterwards, proof that the data left
the cache. The program repeats this check at start-up and saves the numbers
in `benchmark_metadata.json`. Reading a big buffer also empties the address
translation cache (TLB), which `CLFLUSH` did not; the report says so. The
cache line size comes from macOS (128 bytes; the Ryzen's was 64).

**Simple hash join.** macOS cannot pin a thread to a core, so the program
asks for the highest quality-of-service class instead, which macOS runs on the
performance cores. The timer is `CLOCK_UPTIME_RAW`. The bucket rule is a
build-time switch so both rules from the history can run:
- default: at least 2 buckets per left row (the rule in the Week 2 report);
- `-DQUARTER_BUCKETS`: about 1 bucket per 4 left rows (commit `d28429e`).

**Threads experiment.** The radix setting is the one that was fastest at
16,000,000 rows in experiment 4 on this Mac, chosen automatically by
`run_experiments.sh` (v2 fixed it at 14 bits, chosen on the cloud server).
Thread counts are 1, 2 and 4 as the assignment asks, plus 8 to show what
happens once threads spill onto the slower efficiency cores. The v2 program
always wrote "thread i on the i-th allowed CPU" into its environment file;
on macOS that line is now left out, because macOS cannot pin threads.
`make check` now also runs the threaded tests under ThreadSanitizer, which
stops on any two threads touching the same memory without coordination.

**Clock cost.** v2 measured the cost of one clock read as the median of
2,001 back-to-back pairs. The M5's clock moves in 41.7 ns steps, so almost
every pair shows no change and the median came out as 0 ns. The M5 copy
averages 1,000,000 back-to-back reads instead; a separate test measured
about 9 ns per read. This matters because the radix join reads the clock
twice per partition inside its timed region (a v2 design choice we kept so
results stay comparable). The report now computes how much of each run
that is instead of stating "under 1%": it is small for the fastest setting
at every size, but large for settings with many tiny partitions.

## Two design flaws in v2, fixed here

`DESIGN-FLAWS.md` explains both in plain words, with numbers from before and
after the fix. In short:
1. The radix join read the clock twice per partition inside its own timing.
   Now the join loop is timed once; `--timing per-partition` shows the old way.
2. The threaded simple join made every insert wait for all earlier memory
   writes, so its one-thread build took 7 times as long and its speedup looked
   too good. Now each insert is one atomic swap; `--insert cas-release` shows
   the old way.
The results and reports from before the fixes are in
`archive/2026-11-22-before-fixes/`. `scripts/run_experiments.sh` also reruns
the old way of each, right after the fixed run, into
`results/04-radix-join-old-timing` and `results/05-parallel-join-old-insert`.
`results/05-parallel-join-repeat` is a second experiment 5 run, 15 minutes
after the official one: it was 5% to 18% faster, which is why times should be
compared within one run.

## Experiment 6: bucket count and hash function

The professor asked how the bucket count was chosen. It never was:
Week 2 used 0.5 rows per bucket and commit `d28429e` changed it to 4, both
without a test. `src/bucket_sweep.c` measures it. Every setting can be
changed from the command line:
- `--loads`: rows per bucket (0.0625 to 8 by default);
- `--sizes`: rows per input;
- `--joins`: simple and/or radix;
- `--hashes`: identity, multiply-low, fibonacci, crc32, mix;
- `--keys`: dense, stride64, random.
The full run makes two folders: `results/06-bucket-sweep` (rows per bucket,
4 sizes, both joins) and `results/06-hash-functions` (5 hash functions times
3 key patterns at 1,048,576 rows). It also measures how evenly each hash
spreads the keys, on the real tables. One report covers both.

## Live lab

`scripts/live.sh` asks which experiment to run and then each of its
settings. Enter keeps the value in brackets. It runs the same C program as
the official run, builds the same report from the CSV files, opens the PDF,
and prints your numbers next to the official run's. Nothing in `results/` or
`reports/` changes; every live run gets its own folder:

```
live/06-bucket-sweep/2026-11-23_101500/results/      raw CSV from the C program
live/06-bucket-sweep/2026-11-23_101500/report.pdf    the report
live/06-bucket-sweep/2026-11-23_101500/settings.txt  what was typed, exact command
live/latest.pdf                                      newest report
```

`scripts/live.sh 6 again` reruns experiment 6 with the last settings.
The CSV files are plain text with a header row, so any tool can read them.

## Report scripts

- `analysis/report_stride_scan.py` and `analysis/report_simple_hash_join.py`
  rebuild the two original PDFs in the same layout, fonts and charts. The
  scripts that made the original PDFs are not in the repository, so these
  copy the layout from the PDFs. Every sentence that quotes a number is
  computed from the data.
- `analysis/analyze_partition.py` (from `../analysis`) and
  `analysis/final_analysis.py` (from v2) have four fixes:
  1. The "cost rises from B bits" point must last: every larger B must also
     stay more than 30% above the flat level. The old rule picked the one-off
     spike at 1 bit and wrote "steps up at 1 bits".
  2. The flat level is the median of 2 to 8 bits.
  3. Captions and chart markers use this machine's page size (16 KiB) and
     cache line size (128 bytes) instead of 4 KiB and 64 bytes.
  4. The final report reads the Mac's page-size label ("default" instead of
     "4k") and quotes thread speedup at 4 threads, one per performance core.
- `analysis/analyze_radix_join.py` (from v2) has two fixes:
  1. Its cost model only read experiment 3 rows labelled "4k" pages, so on
     the Mac it silently ignored experiment 3. It now also reads "default".
  2. The clock-read share is computed from the data (see "Clock cost").
- `analysis/report_lib.py`: the "Interference" line no longer talks about
  cloud-server steal time, which cannot happen on a laptop. With
  `REPORT_ECHO=1` a report also prints its summary and key numbers.
- `analysis/analyze_bucket_sweep.py` (new): report 6.
- `analysis/live_summary.py` (new): the live lab's comparison with the
  official run.
- `analysis/analyze_parallel.py` and `analysis/analyze_radix_join.py` describe
  whichever insert or timing method the run used.

## Limits on this machine

- No hardware miss counters: macOS does not let a program read them.
- No huge pages: macOS does not let a program choose its page size, so the
  page-size half of experiment 3 is skipped.
- No thread pinning: macOS decides which core runs each thread.

## Explainer pages

`artifacts/` holds the two web pages made from these results. Double-click a
file to open it in a browser; no login or server is needed. The fonts load
from Google Fonts when online; offline the pages fall back to the system font.
- `how-we-test-joins.html`: what each of the six experiments asks, how it is
  set up, and what it measured. Built by
  `python3 artifacts/source/make_design_page.py results artifacts/how-we-test-joins.html`
  from `artifacts/source/design_template.html`; every number on it comes from
  `results/`.
- `hash-bucket-lab.html`: how the hash join files rows into boxes, how evenly
  `mix` spreads our keys, and the two bucket rules timed on the M5. Its lab
  section rebuilds a hash table in the browser as you change the settings.

The same two pages are also published online (private to the owner):
[How We Test Joins](https://claude.ai/artifact/Voi2wNcx9smsHLuSjdSPt9) and
[Hash Bucket Lab](https://claude.ai/artifact/7uvb4pBjwgHkgY16CNVLsE).
