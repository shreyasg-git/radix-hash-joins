/* Radix partitioning: multi-pass radix-cluster.
 *
 * Question: how much does one partitioning pass cost per record as the number
 * of output partitions grows, and does splitting the same total number of
 * radix bits over several passes help, as Boncz, Manegold and Kersten argue?
 *
 * For each setting we partition N records on B radix bits using P passes.
 * Pass p writes into 2^(B/P) clusters at a time. When that fan-out is larger
 * than the TLB (pages it can translate quickly) or the number of cache lines
 * the CPU can keep open for writing, every write tends to miss. More passes
 * keep the fan-out small, at the cost of reading and writing the data again.
 *
 * Optional second factor: 4 KiB pages versus 2 MiB huge pages. Huge pages
 * need far fewer TLB entries for the same memory, a modern feature that can
 * weaken the TLB effect the paper measured. Linux only: macOS does not let a
 * program choose its page size, so there the default pages are used.
 *
 * Build:  make            (or see the Makefile)
 * Test:   ./bin/radix_partition --test
 * Run:    ./bin/radix_partition results/radix-partitioning [options]
 * Options: --rows N  --repeats R  --max-bits B  --passes 1,2,3  --pages 4k,2m  --seed S
 *
 * Each round runs every setting once, in a newly shuffled order. The CPU time the
 * hypervisor took away during each measurement (steal) is recorded too.
 */
#include "common.h"

#define MAX_BITS 24
#define MAX_PASSES 4

typedef struct {
    Record *input, *buffer_a, *buffer_b;
    size_t *start_a, *start_b; /* cluster bounds, 2^MAX_BITS + 1 entries each */
} Buffers;

typedef struct {
    Record *output;
    const size_t *start; /* bounds of the final 2^B clusters */
} Partitioned;

/* Runs all passes for one setting. Fills per-pass times and counter deltas. */
static Partitioned partition(Buffers *b, size_t rows, unsigned total_bits, unsigned passes,
                             PassTime *times, CounterValues *before, CounterValues *after,
                             const Counters *counters) {
    unsigned bits[MAX_PASSES];
    split_bits(total_bits, passes, bits);
    const Record *src = b->input;
    Record *dst = b->buffer_a;
    size_t first[2] = {0, rows};
    const size_t *start = first;
    size_t *new_start = b->start_a;
    size_t clusters = 1;
    unsigned remaining = total_bits;
    for (unsigned p = 0; p < passes; ++p) {
        remaining -= bits[p];
        if (before) before[p] = counters_read(counters);
        times[p] = radix_pass(src, dst, start, clusters, remaining, bits[p], new_start);
        if (after) after[p] = counters_read(counters);
        clusters <<= bits[p];
        start = new_start;
        new_start = (new_start == b->start_a) ? b->start_b : b->start_a;
        src = dst;
        dst = (dst == b->buffer_a) ? b->buffer_b : b->buffer_a;
    }
    return (Partitioned){(Record *)src, start};
}

/* ---------- correctness ------------------------------------------------- */

static int compare_records(const void *first, const void *second) {
    const Record *a = first, *b = second;
    if (a->key != b->key) return a->key < b->key ? -1 : 1;
    return (a->row_id > b->row_id) - (a->row_id < b->row_id);
}

/* Every record in final cluster c must have radix value c, the clusters must
 * cover all rows, and the output must contain exactly the input records. */
static int check_partitioned(const Record *input, Partitioned out, size_t rows, unsigned total_bits,
                             int compare_exactly) {
    size_t clusters = (size_t)1 << total_bits;
    if (out.start[0] != 0 || out.start[clusters] != rows) return 0;
    uint64_t sum_in = 0, sum_out = 0;
    for (size_t c = 0; c < clusters; ++c) {
        if (out.start[c] > out.start[c + 1]) return 0;
        for (size_t i = out.start[c]; i < out.start[c + 1]; ++i) {
            if (radix_of(out.output[i].key, 0, total_bits) != c) return 0;
            sum_out += pair_hash(out.output[i].key, out.output[i].row_id);
        }
    }
    for (size_t i = 0; i < rows; ++i) sum_in += pair_hash(input[i].key, input[i].row_id);
    if (sum_in != sum_out) return 0;
    if (compare_exactly) {
        Record *x = allocate(rows, sizeof(Record)), *y = allocate(rows, sizeof(Record));
        memcpy(x, input, rows * sizeof(Record));
        memcpy(y, out.output, rows * sizeof(Record));
        qsort(x, rows, sizeof(Record), compare_records);
        qsort(y, rows, sizeof(Record), compare_records);
        int same = !memcmp(x, y, rows * sizeof(Record));
        free(x);
        free(y);
        if (!same) return 0;
    }
    return 1;
}

static Buffers make_buffers(size_t rows, unsigned max_bits, PageMode mode) {
    Buffers b;
    size_t bytes = (rows ? rows : 1) * sizeof(Record);
    b.input = allocate_touched(bytes, mode);
    b.buffer_a = allocate_touched(bytes, mode);
    b.buffer_b = allocate_touched(bytes, mode);
    b.start_a = allocate_touched((((size_t)1 << max_bits) + 1) * sizeof(size_t), PAGES_DEFAULT);
    b.start_b = allocate_touched((((size_t)1 << max_bits) + 1) * sizeof(size_t), PAGES_DEFAULT);
    return b;
}

static void free_buffers(Buffers *b) {
    free(b->input); free(b->buffer_a); free(b->buffer_b); free(b->start_a); free(b->start_b);
}

static void self_test(void) {
    const size_t sizes[] = {0, 1, 2, 37, 1000, 4096, 100003};
    size_t cases = 0;
    uint64_t state = 99;
    for (size_t s = 0; s < sizeof(sizes) / sizeof(sizes[0]); ++s) {
        size_t rows = sizes[s];
        Buffers b = make_buffers(rows, 12, PAGES_DEFAULT);
        for (size_t i = 0; i < rows; ++i)  /* include duplicate keys and the largest key */
            b.input[i] = (Record){i % 7 == 0 ? UINT32_MAX : (uint32_t)(random_next(&state) % (rows + 1)), (uint32_t)i};
        for (unsigned passes = 1; passes <= 3; ++passes) {
            for (unsigned bits = passes == 1 ? 0 : passes; bits <= 12; ++bits) {
                PassTime times[MAX_PASSES];
                Partitioned out = partition(&b, rows, bits, passes, times, NULL, NULL, NULL);
                if (!check_partitioned(b.input, out, rows, bits, 1)) {
                    fprintf(stderr, "FAIL: rows=%zu bits=%u passes=%u\n", rows, bits, passes);
                    exit(EXIT_FAILURE);
                }
                ++cases;
            }
        }
        free_buffers(&b);
    }
    printf("Passed %zu partitioning cases (sizes 0 to 100003, 0 to 12 bits, 1 to 3 passes).\n", cases);
}

/* ---------- benchmark --------------------------------------------------- */

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--test")) { self_test(); return 0; }
    if (argc < 2 || argv[1][0] == '-') {
        fprintf(stderr, "Usage: %s --test\n       %s OUTPUT_DIRECTORY [--rows N] [--repeats R] "
                "[--max-bits B] [--passes 1,2,3] [--pages 4k,2m] [--seed S]\n", argv[0], argv[0]);
        return EXIT_FAILURE;
    }
    size_t rows = (size_t)1 << 24, repeats = 15, max_bits = 20, seed = 42;
    size_t pass_list[MAX_PASSES] = {1, 2, 3}, pass_count = 3;
#ifdef __linux__
    PageMode modes[2] = {PAGES_4K, PAGES_2M};
    size_t mode_count = 2;
#else
    PageMode modes[2] = {PAGES_DEFAULT, PAGES_DEFAULT}; /* page size cannot be chosen on macOS */
    size_t mode_count = 1;
#endif
    for (int i = 2; i < argc; i += 2) {
        if (i + 1 >= argc) die("Missing value after option");
        if (!strcmp(argv[i], "--rows")) rows = parse_number(argv[i + 1], 1024, (size_t)1 << 30);
        else if (!strcmp(argv[i], "--repeats")) repeats = parse_number(argv[i + 1], 1, 100);
        else if (!strcmp(argv[i], "--seed")) seed = parse_number(argv[i + 1], 0, UINT32_MAX);
        else if (!strcmp(argv[i], "--max-bits")) max_bits = parse_number(argv[i + 1], 1, MAX_BITS);
        else if (!strcmp(argv[i], "--passes")) pass_count = parse_list(argv[i + 1], pass_list, MAX_PASSES, 1, MAX_PASSES);
        else if (!strcmp(argv[i], "--pages")) {
            mode_count = 0;
            if (strstr(argv[i + 1], "4k")) modes[mode_count++] = PAGES_4K;
            if (strstr(argv[i + 1], "2m")) modes[mode_count++] = PAGES_2M;
            if (!mode_count) die("--pages takes 4k, 2m or 4k,2m");
        } else die("Unknown option");
    }
    make_results_directory(argv[1]);
    int cpu = pin_to_cpu(0);
    Counters counters;
    counters_open(&counters);

    FILE *env = open_in(argv[1], "environment.txt");
    write_common_environment(env);
    fprintf(env, "experiment=radix_partition\nlogical_cpu=%d\nrows=%zu\nrepeats=%zu\nwarmup_runs=1\nmax_bits=%zu\n"
            "seed=%zu\nrun_order=shuffled every round\n", cpu, rows, repeats, max_bits, seed);
    counters_describe(env, &counters);
    fprintf(env, "timer_call_ns=%.1f\n", timer_cost_ns());

    FILE *out = open_in(argv[1], "timings.csv");
    fprintf(out, "page_mode,rows,passes,total_bits,pass,pass_bits,clusters_after_pass,run,position,steal_ms,histogram_ms,scatter_ms,pass_ms");
    counters_header(out, "");
    fputc('\n', out);

    for (size_t m = 0; m < mode_count; ++m) {
        Buffers b = make_buffers(rows, (unsigned)max_bits, modes[m]);
        fprintf(env, "anon_huge_kb_after_allocating_%s=%ld\n", page_mode_name[modes[m]], anon_huge_kb());
        Record *unused_right = allocate(rows, sizeof(Record));
        generate(b.input, unused_right, rows, seed); /* left input of the simple hash join (seed 42) */
        free(unused_right);
        /* Every (passes, bits) setting of this page mode. */
        unsigned cfg_passes[MAX_PASSES * (MAX_BITS + 1)], cfg_bits[MAX_PASSES * (MAX_BITS + 1)];
        size_t cfg_count = 0;
        for (size_t pi = 0; pi < pass_count; ++pi)
            for (unsigned bits = pass_list[pi] == 1 ? 0 : (unsigned)pass_list[pi]; bits <= max_bits; ++bits) {
                cfg_passes[cfg_count] = (unsigned)pass_list[pi];
                cfg_bits[cfg_count++] = bits;
            }
        size_t order[MAX_PASSES * (MAX_BITS + 1)];
        uint64_t order_state = 1000 + modes[m];
        for (size_t run = 0; run <= repeats; ++run) { /* run 0 is the warmup and is verified */
            shuffle_order(order, cfg_count, &order_state);
            for (size_t position = 0; position < cfg_count; ++position) {
                unsigned passes = cfg_passes[order[position]], bits = cfg_bits[order[position]];
                {
                    PassTime times[MAX_PASSES];
                    CounterValues before[MAX_PASSES], after[MAX_PASSES];
                    double steal_before = steal_ms(cpu);
                    Partitioned result = partition(&b, rows, bits, passes, times, before, after, &counters);
                    double stolen = steal_delta(steal_before, steal_ms(cpu));
                    if (run == 0) {
                        if (!check_partitioned(b.input, result, rows, bits, 0)) die("Partitioning check failed");
                        continue;
                    }
                    unsigned split[MAX_PASSES];
                    split_bits(bits, passes, split);
                    size_t clusters = 1;
                    for (unsigned p = 0; p < passes; ++p) {
                        clusters <<= split[p];
                        fprintf(out, "%s,%zu,%u,%u,%u,%u,%zu,%zu,%zu,%.1f,%.6f,%.6f,%.6f", page_mode_name[modes[m]],
                                rows, passes, bits, p + 1, split[p], clusters, run, position, stolen,
                                times[p].histogram_ns / 1e6, times[p].scatter_ns / 1e6,
                                (times[p].histogram_ns + times[p].scatter_ns) / 1e6);
                        counters_write(out, before[p], after[p]);
                        fputc('\n', out);
                    }
                }
            }
            fflush(out);
            printf("%s pages: %s %zu of %zu\n", page_mode_name[modes[m]], run ? "recorded run" : "verified warmup",
                   run, repeats);
            fflush(stdout);
        }
        free_buffers(&b);
    }
    fclose(out);
    fclose(env);
    return 0;
}
