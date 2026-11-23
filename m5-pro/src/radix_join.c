/* Experiment 4: simple hash join versus radix hash join.
 *
 * Simple hash join (the Week 2 baseline): build one hash table on all of R,
 * then probe it with every row of S. Once that table is larger than the
 * caches, most probes go to main memory.
 *
 * Radix hash join (Boncz, Manegold and Kersten): first partition R and S on
 * the same B radix bits (in P passes), then join each pair of partitions
 * R_p with S_p with a small hash table that should fit in cache. Partitioning
 * costs extra passes over the data; the question is when it pays for itself.
 *
 * Both joins share the same hash table code, record layout, input data, output
 * policy (count matches + checksum) and timing rules:
 *   - every buffer is allocated and first-touched BEFORE the timer starts;
 *   - filling the bucket array with EMPTY is part of build time;
 *   - partition, build and probe are timed separately, total covers all.
 *
 * Build:  make
 * Test:   ./bin/radix_join --test
 * Run:    ./bin/radix_join results/04-radix-join [options]
 * Options: --sizes 1000000,4000000  --bits 0,4,8,12  --passes 1,2  --repeats R  --seed S
 *          (bits 0 means the simple hash join)
 *
 * Each round runs every setting of one input size once, in a newly shuffled
 * order. The CPU time the hypervisor took away (steal) is recorded per run.
 *
 * M5 copy, timing fix (see DESIGN-FLAWS.md, flaw 1). v2 read the clock twice
 * per partition inside the timed join, about 9 ns per read on the M5. With
 * 2^14 partitions that is 0.3 ms, 18% of a 100,000-row run. Now the whole
 * build-and-probe loop is timed once, with no clock reads inside it. The
 * build/probe split comes from a separate build-only pass right after each
 * run (not part of the total): build = that pass, probe = join - build.
 * --timing per-partition brings back the v2 way, to show the difference.
 */
#include "common.h"

#define MAX_BITS 22
#define MAX_PASSES 3

typedef struct {
    Record *a, *b;          /* two work buffers for partitioning passes */
    size_t *start_a, *start_b;
} PartitionBuffers;

static int clock_per_partition = 0; /* --timing per-partition: the v2 way */

typedef struct {
    double partition_r_ms, partition_s_ms, build_ms, probe_ms, join_ms, total_ms;
    size_t max_partition_rows;
    JoinResult result;
} JoinTimes;

/* All passes of the radix partitioning. Returns the buffer with the result and
 * sets *bounds to the 2^bits + 1 cluster bounds. */
static Record *partition_all(const Record *input, size_t rows, unsigned bits, unsigned passes,
                             PartitionBuffers *pb, const size_t **bounds) {
    unsigned split[MAX_PASSES];
    split_bits(bits, passes, split);
    static size_t whole[2];
    whole[0] = 0;
    whole[1] = rows;
    const Record *src = input;
    Record *dst = pb->a;
    const size_t *start = whole;
    size_t *new_start = pb->start_a;
    size_t clusters = 1;
    unsigned remaining = bits;
    for (unsigned p = 0; p < passes; ++p) {
        remaining -= split[p];
        radix_pass(src, dst, start, clusters, remaining, split[p], new_start);
        clusters <<= split[p];
        start = new_start;
        new_start = new_start == pb->start_a ? pb->start_b : pb->start_a;
        src = dst;
        dst = dst == pb->a ? pb->b : pb->a;
    }
    *bounds = start;
    return (Record *)src;
}

static JoinTimes simple_join(const Record *left, size_t left_rows, const Record *right, size_t right_rows,
                             uint32_t *buckets, uint32_t *next) {
    JoinTimes t = {0};
    HashTable table;
    uint64_t start = now_ns();
    hash_build(&table, left, left_rows, buckets, next, 0);
    uint64_t built = now_ns();
    hash_probe(&table, right, right_rows, &t.result);
    uint64_t done = now_ns();
    t.build_ms = (built - start) / 1e6;
    t.probe_ms = (done - built) / 1e6;
    t.join_ms = (done - start) / 1e6;
    t.total_ms = (done - start) / 1e6;
    t.max_partition_rows = left_rows;
    return t;
}

static JoinTimes radix_join(const Record *left, size_t left_rows, const Record *right, size_t right_rows,
                            unsigned bits, unsigned passes, PartitionBuffers *pr, PartitionBuffers *ps,
                            uint32_t *buckets, uint32_t *next, Counters *counters,
                            CounterValues *c0, CounterValues *c1, CounterValues *c2) {
    JoinTimes t = {0};
    const size_t *rb, *sb;
    if (c0) *c0 = counters_read(counters);
    uint64_t start = now_ns();
    const Record *r = partition_all(left, left_rows, bits, passes, pr, &rb);
    uint64_t r_done = now_ns();
    const Record *s = partition_all(right, right_rows, bits, passes, ps, &sb);
    uint64_t s_done = now_ns();
    if (c1) *c1 = counters_read(counters);
    /* Join partition p of R with partition p of S. Both partitions hold keys
     * with the same low `bits` hash bits, so buckets use the bits above them. */
    size_t partitions = (size_t)1 << bits;
    uint64_t build_ns = 0, probe_ns = 0, join_start = now_ns(), done;
    if (clock_per_partition) { /* v2: two clock reads per partition inside the timed loop */
        uint64_t before = join_start;
        for (size_t p = 0; p < partitions; ++p) {
            size_t r_rows = rb[p + 1] - rb[p];
            if (r_rows > t.max_partition_rows) t.max_partition_rows = r_rows;
            HashTable table;
            hash_build(&table, r + rb[p], r_rows, buckets, next, bits);
            uint64_t built = now_ns();
            hash_probe(&table, s + sb[p], sb[p + 1] - sb[p], &t.result);
            uint64_t probed = now_ns();
            build_ns += built - before;
            probe_ns += probed - built;
            before = probed;
        }
        done = now_ns();
    } else {
        for (size_t p = 0; p < partitions; ++p) {
            size_t r_rows = rb[p + 1] - rb[p];
            if (r_rows > t.max_partition_rows) t.max_partition_rows = r_rows;
            HashTable table;
            hash_build(&table, r + rb[p], r_rows, buckets, next, bits);
            hash_probe(&table, s + sb[p], sb[p + 1] - sb[p], &t.result);
        }
        done = now_ns();
        /* Build-only pass for the phase split; it is not part of the total. */
        uint64_t build_start = now_ns();
        for (size_t p = 0; p < partitions; ++p) {
            HashTable table;
            hash_build(&table, r + rb[p], rb[p + 1] - rb[p], buckets, next, bits);
        }
        build_ns = now_ns() - build_start;
        probe_ns = done - join_start > build_ns ? done - join_start - build_ns : 0;
    }
    if (c2) *c2 = counters_read(counters);
    t.partition_r_ms = (r_done - start) / 1e6;
    t.partition_s_ms = (s_done - r_done) / 1e6;
    t.build_ms = build_ns / 1e6;
    t.probe_ms = probe_ns / 1e6;
    t.join_ms = (done - join_start) / 1e6;
    t.total_ms = (done - start) / 1e6;
    return t;
}

static PartitionBuffers make_partition_buffers(size_t rows, unsigned max_bits) {
    PartitionBuffers pb;
    pb.a = allocate_touched((rows ? rows : 1) * sizeof(Record), PAGES_DEFAULT);
    pb.b = allocate_touched((rows ? rows : 1) * sizeof(Record), PAGES_DEFAULT);
    pb.start_a = allocate_touched((((size_t)1 << max_bits) + 1) * sizeof(size_t), PAGES_DEFAULT);
    pb.start_b = allocate_touched((((size_t)1 << max_bits) + 1) * sizeof(size_t), PAGES_DEFAULT);
    return pb;
}

static void free_partition_buffers(PartitionBuffers *pb) {
    free(pb->a); free(pb->b); free(pb->start_a); free(pb->start_b);
}

/* ---------- correctness ------------------------------------------------- */

static JoinResult nested_loop(const Record *left, size_t ln, const Record *right, size_t rn) {
    JoinResult result = {0, 0};
    for (size_t j = 0; j < rn; ++j)
        for (size_t i = 0; i < ln; ++i)
            if (left[i].key == right[j].key) {
                ++result.matches;
                result.checksum += pair_hash(left[i].row_id, right[j].row_id);
            }
    return result;
}

static void check(const char *name, const Record *left, size_t ln, const Record *right, size_t rn, size_t *cases) {
    JoinResult expected = nested_loop(left, ln, right, rn);
    size_t most = ln > rn ? ln : rn;
    PartitionBuffers pr = make_partition_buffers(most, 8), ps = make_partition_buffers(most, 8);
    uint32_t *buckets = allocate(bucket_count_for(most ? most : 1), sizeof(uint32_t));
    uint32_t *next = allocate(most ? most : 1, sizeof(uint32_t));
    JoinTimes t = simple_join(left, ln, right, rn, buckets, next);
    if (t.result.matches != expected.matches || t.result.checksum != expected.checksum) {
        fprintf(stderr, "FAIL simple: %s\n", name);
        exit(EXIT_FAILURE);
    }
    ++*cases;
    for (int mode = 0; mode < 2; ++mode) {
        clock_per_partition = mode;
        for (unsigned passes = 1; passes <= 3; ++passes) {
            for (unsigned bits = passes; bits <= 8; ++bits) {
                t = radix_join(left, ln, right, rn, bits, passes, &pr, &ps, buckets, next, NULL, NULL, NULL, NULL);
                if (t.result.matches != expected.matches || t.result.checksum != expected.checksum) {
                    fprintf(stderr, "FAIL radix bits=%u passes=%u timing=%d: %s\n", bits, passes, mode, name);
                    exit(EXIT_FAILURE);
                }
                ++*cases;
            }
        }
    }
    clock_per_partition = 0;
    free_partition_buffers(&pr); free_partition_buffers(&ps); free(buckets); free(next);
}

static void self_test(void) {
    size_t cases = 0;
    Record left[] = {{7, 0}, {7, 1}, {9, 2}, {UINT32_MAX, 3}, {0, 4}};
    Record right[] = {{7, 0}, {7, 1}, {0, 2}, {UINT32_MAX, 3}, {42, 4}};
    Record other[] = {{22, 0}, {23, 1}};
    check("duplicates and boundary keys", left, 5, right, 5, &cases);
    check("no matches", left, 5, other, 2, &cases);
    check("empty left", NULL, 0, right, 5, &cases);
    check("empty right", left, 5, NULL, 0, &cases);
    check("both empty", NULL, 0, NULL, 0, &cases);
    uint64_t state = 17;
    for (size_t trial = 0; trial < 100; ++trial) {  /* random sizes, many duplicates */
        size_t ln = 1 + random_next(&state) % 300, rn = 1 + random_next(&state) % 300;
        uint32_t range = 1 + (uint32_t)(random_next(&state) % 200);
        Record *a = allocate(ln, sizeof(Record)), *b = allocate(rn, sizeof(Record));
        for (size_t i = 0; i < ln; ++i) a[i] = (Record){(uint32_t)(random_next(&state) % range), (uint32_t)i};
        for (size_t i = 0; i < rn; ++i) b[i] = (Record){(uint32_t)(random_next(&state) % range), (uint32_t)i};
        check("random duplicate keys", a, ln, b, rn, &cases);
        free(a); free(b);
    }
    /* Generated benchmark data at a small size: known answer. */
    size_t n = 5000;
    Record *l = allocate(n, sizeof(Record)), *r = allocate(n, sizeof(Record));
    generate(l, r, n, 42);
    JoinResult want = expected_result(n), got = nested_loop(l, n, r, n);
    if (want.matches != got.matches || want.checksum != got.checksum) die("FAIL generated data");
    check("generated benchmark data", l, n, r, n, &cases);
    free(l); free(r);
    printf("Passed %zu join checks against the nested-loop reference "
           "(simple join, and radix join with 1 to 8 bits in 1 to 3 passes, both timing modes).\n", cases);
}

/* ---------- benchmark --------------------------------------------------- */

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--test")) { self_test(); return 0; }
    if (argc < 2 || argv[1][0] == '-') {
        fprintf(stderr, "Usage: %s --test\n       %s OUTPUT_DIRECTORY [--sizes N,N] [--bits B,B] "
                "[--passes 1,2] [--repeats R] [--seed S] [--timing clean|per-partition]\n", argv[0], argv[0]);
        return EXIT_FAILURE;
    }
    size_t sizes[16] = {100000, 1000000, 2000000, 4000000, 8000000, 16000000, 32000000}, size_count = 7;
    size_t bit_list[32] = {0, 2, 4, 6, 8, 10, 12, 14, 16, 18}, bit_count = 10;
    size_t pass_list[MAX_PASSES] = {1, 2}, pass_count = 2, repeats = 15, seed = 42;
    for (int i = 2; i < argc; i += 2) {
        if (i + 1 >= argc) die("Missing value after option");
        if (!strcmp(argv[i], "--sizes")) size_count = parse_list(argv[i + 1], sizes, 16, 1000, (size_t)1 << 30);
        else if (!strcmp(argv[i], "--bits")) bit_count = parse_list(argv[i + 1], bit_list, 32, 0, MAX_BITS);
        else if (!strcmp(argv[i], "--passes")) pass_count = parse_list(argv[i + 1], pass_list, MAX_PASSES, 1, MAX_PASSES);
        else if (!strcmp(argv[i], "--repeats")) repeats = parse_number(argv[i + 1], 1, 100);
        else if (!strcmp(argv[i], "--seed")) seed = parse_number(argv[i + 1], 0, UINT32_MAX);
        else if (!strcmp(argv[i], "--timing")) {
            if (!strcmp(argv[i + 1], "per-partition")) clock_per_partition = 1;
            else if (strcmp(argv[i + 1], "clean")) die("--timing must be clean or per-partition");
        }
        else die("Unknown option");
    }
    unsigned max_bits = 0;
    for (size_t i = 0; i < bit_count; ++i) if (bit_list[i] > max_bits) max_bits = (unsigned)bit_list[i];

    make_results_directory(argv[1]);
    int cpu = pin_to_cpu(0);
    Counters counters;
    counters_open(&counters);
    FILE *env = open_in(argv[1], "environment.txt");
    write_common_environment(env);
    fprintf(env, "experiment=radix_join\nlogical_cpu=%d\nrepeats=%zu\nwarmup_runs=1\nseed=%zu\n"
            "run_order=shuffled every round\n", cpu, repeats, seed);
    counters_describe(env, &counters);
    fprintf(env, "timer_call_ns=%.1f\n", timer_cost_ns());
    fputs(clock_per_partition
          ? "timing=per-partition (v2: two clock reads per partition inside the timed join)\n"
          : "timing=clean (no clock reads inside the join; build split from a separate build-only pass, "
            "probe = join - build)\n", env);
    fclose(env);

    FILE *out = open_in(argv[1], "timings.csv");
    fputs("rows_per_input,algorithm,total_bits,passes,run,position,steal_ms,partitions,max_partition_rows,"
          "partition_r_ms,partition_s_ms,build_ms,probe_ms,join_ms,total_ms,matches", out);
    counters_header(out, "partition_");
    counters_header(out, "join_");
    fputc('\n', out);

    for (size_t si = 0; si < size_count; ++si) {
        size_t rows = sizes[si];
        Record *left = allocate_touched(rows * sizeof(Record), PAGES_DEFAULT);
        Record *right = allocate_touched(rows * sizeof(Record), PAGES_DEFAULT);
        generate(left, right, rows, seed);
        JoinResult expected = expected_result(rows);
        PartitionBuffers pr = make_partition_buffers(rows, max_bits), ps = make_partition_buffers(rows, max_bits);
        uint32_t *buckets = allocate_touched(bucket_count_for(rows) * sizeof(uint32_t), PAGES_DEFAULT);
        uint32_t *next = allocate_touched(rows * sizeof(uint32_t), PAGES_DEFAULT);
        /* Every setting for this size; bits 0 is the simple join. */
        unsigned cfg_bits[64], cfg_passes[64];
        size_t cfg_count = 0;
        for (size_t bi = 0; bi < bit_count; ++bi) {
            unsigned bits = (unsigned)bit_list[bi];
            if (bits && ((size_t)1 << bits) > rows / 4) continue; /* fewer than ~4 rows per partition */
            for (size_t pi = 0; pi < pass_count; ++pi) {
                unsigned passes = (unsigned)pass_list[pi];
                if (bits == 0 && pi > 0) break;          /* the simple join has no passes */
                if (bits && passes > bits) continue;
                if (cfg_count == 64) die("Too many settings");
                cfg_bits[cfg_count] = bits;
                cfg_passes[cfg_count++] = bits ? passes : 0;
            }
        }
        size_t order[64];
        uint64_t order_state = 2000 + rows;
        for (size_t run = 0; run <= repeats; ++run) { /* run 0 is a warmup */
            shuffle_order(order, cfg_count, &order_state);
            for (size_t position = 0; position < cfg_count; ++position) {
                unsigned bits = cfg_bits[order[position]], passes = cfg_passes[order[position]];
                CounterValues c0, c1, c2;
                JoinTimes t;
                double steal_before = steal_ms(cpu);
                if (bits == 0) {
                    c0 = c1 = counters_read(&counters);
                    t = simple_join(left, rows, right, rows, buckets, next);
                    c2 = counters_read(&counters);
                } else {
                    t = radix_join(left, rows, right, rows, bits, passes, &pr, &ps, buckets, next,
                                   &counters, &c0, &c1, &c2);
                }
                double stolen = steal_delta(steal_before, steal_ms(cpu));
                if (t.result.matches != expected.matches || t.result.checksum != expected.checksum)
                    die("Join result does not match the expected answer");
                if (!run) continue;
                fprintf(out, "%zu,%s,%u,%u,%zu,%zu,%.1f,%zu,%zu,%.6f,%.6f,%.6f,%.6f,%.6f,%.6f,%" PRIu64, rows,
                        bits ? "radix" : "simple", bits, passes, run, position, stolen, (size_t)1 << bits,
                        t.max_partition_rows, t.partition_r_ms, t.partition_s_ms, t.build_ms, t.probe_ms,
                        t.join_ms, t.total_ms, t.result.matches);
                counters_write(out, c0, c1);
                counters_write(out, c1, c2);
                fputc('\n', out);
            }
            fflush(out);
        }
        printf("%zu rows per input: every run verified (%" PRIu64 " matches)\n", rows, expected.matches);
        fflush(stdout);
        free(left); free(right); free(buckets); free(next);
        free_partition_buffers(&pr); free_partition_buffers(&ps);
    }
    fclose(out);
    return 0;
}
