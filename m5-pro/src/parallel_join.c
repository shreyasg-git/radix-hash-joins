/* Experiment 5: parallel simple and radix hash joins with 1, 2 and 4 threads.
 *
 * Simple join, in parallel:
 *   build  - every thread inserts its share of R into ONE shared hash table,
 *            using an atomic swap on the bucket head (see the M5 note below);
 *   probe  - every thread probes the shared table with its share of S.
 *
 * Radix join, in parallel:
 *   partition - pass 1: each thread counts its share of the input, the counts
 *               are combined so each thread knows where to write, then every
 *               thread scatters its share (no locks). Later passes: whole
 *               clusters are handed out to threads, which split them alone.
 *               Done for R and then for S.
 *   build     - partitions are handed out; each gets its own small hash table.
 *   probe     - partitions are handed out again; S_p probes the table of R_p.
 * All threads wait at a barrier after every phase, so each phase has a clean
 * wall-clock time. Work is handed out with one shared atomic counter.
 *
 * Note: the radix build and probe are separate phases here (all tables are
 * built, then all are probed) so their times can be reported separately with
 * threads. Each small table is read again from memory at the start of its
 * probe, which costs a little compared with the one-thread program.
 *
 * Build:  make
 * Test:   ./bin/parallel_join --test
 * Run:    ./bin/parallel_join results/05-parallel-join [--rows N] [--threads 1,2,4]
 *                             [--bits B] [--passes P] [--repeats R] [--seed S]
 *
 * Each round runs every (threads, join) combination once, in a newly shuffled
 * order. The CPU time the hypervisor took from all CPUs (steal) is recorded.
 *
 * M5 copy, insert fix (see DESIGN-FLAWS.md, flaw 2). v2 inserted with a
 * compare-and-swap in "release" order: before each insert the core had to
 * finish every earlier memory write. On the M5 that made the one-thread build
 * take 540 ms instead of 80 ms, so the simple join's speedup with more threads
 * looked better than it is. Now each insert is one atomic swap with no
 * ordering request: old = swap(head, i); next[i] = old. That is safe because
 * no thread reads the table until every thread has passed the barrier after
 * the build, and the barrier (a mutex) makes all writes visible.
 * --insert cas-release brings back the v2 way, to show the difference.
 */
#include "common.h"
#include <pthread.h>

#define MAX_THREADS 16
#define MAX_PASSES 3
#define BATCH 16 /* partitions taken from the shared counter at a time */

/* A small portable barrier (macOS has no pthread_barrier_t). */
typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t cond;
    int count, waiting, generation;
} Barrier;

static void barrier_init(Barrier *b, int count) {
    pthread_mutex_init(&b->mutex, NULL);
    pthread_cond_init(&b->cond, NULL);
    b->count = count;
    b->waiting = 0;
    b->generation = 0;
}

static void barrier_wait(Barrier *b) {
    pthread_mutex_lock(&b->mutex);
    int generation = b->generation;
    if (++b->waiting == b->count) {
        b->waiting = 0;
        ++b->generation;
        pthread_cond_broadcast(&b->cond);
    } else {
        while (generation == b->generation) pthread_cond_wait(&b->cond, &b->mutex);
    }
    pthread_mutex_unlock(&b->mutex);
}

static void barrier_destroy(Barrier *b) {
    pthread_mutex_destroy(&b->mutex);
    pthread_cond_destroy(&b->cond);
}

/* One input being partitioned: its source and two work buffers. */
typedef struct {
    const Record *input;
    size_t rows;
    Record *buffer[2];
    size_t *bounds[2];      /* cluster bounds after a pass, 2^bits + 1 entries */
    const Record *result;   /* filled in after the last pass */
    const size_t *result_bounds;
} Side;

static int insert_cas_release = 0; /* --insert cas-release: the v2 way */

typedef struct {
    int threads, radix;
    unsigned bits, passes, split[MAX_PASSES];
    Side r, s;
    size_t *histogram;       /* threads x 2^split[0] counts for pass 1 */
    size_t *scratch;         /* threads x (2^max pass bits + 1), private bounds for later passes */
    uint32_t *buckets;       /* simple: one table; radix: all small tables back to back */
    size_t bucket_count;     /* simple join only */
    size_t *bucket_offset;   /* radix: where partition p's buckets start, 2^bits + 1 entries */
    uint32_t *next;          /* chain links, one per row of R */
    size_t task;             /* shared work counter, used with atomic fetch-add */
    Barrier barrier;
    uint64_t stamp[6];       /* phase end times, written by thread 0 */
    JoinResult result[MAX_THREADS];
} Shared;

typedef struct {
    Shared *shared;
    int id;
} Worker;

static void phase_end(Shared *sh, int id, int index) {
    barrier_wait(&sh->barrier);
    if (id == 0) sh->stamp[index] = now_ns();
}

static size_t share_begin(size_t rows, int id, int threads) { return rows * (size_t)id / (size_t)threads; }

/* ---------- parallel radix partitioning --------------------------------- */

static void partition_side(Shared *sh, Side *side, int id) {
    const int T = sh->threads;
    unsigned remaining = sh->bits - sh->split[0];
    const size_t fanout = (size_t)1 << sh->split[0];
    size_t begin = share_begin(side->rows, id, T), end = share_begin(side->rows, id + 1, T);
    size_t *mine = sh->histogram + (size_t)id * fanout;
    /* Pass 1, step 1: count my rows per cluster. */
    for (size_t j = 0; j < fanout; ++j) mine[j] = 0;
    for (size_t i = begin; i < end; ++i) ++mine[radix_of(side->input[i].key, remaining, sh->split[0])];
    barrier_wait(&sh->barrier);
    /* Step 2: my first slot in cluster j = rows of all clusters before j
     * + rows that threads before me put into cluster j. */
    size_t *position = sh->scratch + (size_t)id * (((size_t)1 << sh->bits) + 1);
    size_t base = 0;
    for (size_t j = 0; j < fanout; ++j) {
        size_t before_me = 0, total = 0;
        for (int t = 0; t < T; ++t) {
            size_t c = sh->histogram[(size_t)t * fanout + j];
            if (t < id) before_me += c;
            total += c;
        }
        position[j] = base + before_me;
        if (id == 0) side->bounds[0][j] = base;
        base += total;
    }
    if (id == 0) side->bounds[0][fanout] = side->rows;
    /* Step 3: copy my rows to their clusters. */
    Record *dst = side->buffer[0];
    for (size_t i = begin; i < end; ++i) dst[position[radix_of(side->input[i].key, remaining, sh->split[0])]++] = side->input[i];
    barrier_wait(&sh->barrier);

    /* Later passes: hand out whole clusters; each thread splits a cluster alone,
     * using private bounds and then copying them into the shared array, so no
     * two threads ever write the same entry. */
    size_t clusters = fanout;
    int from = 0;
    for (unsigned p = 1; p < sh->passes; ++p) {
        remaining -= sh->split[p];
        const size_t sub = (size_t)1 << sh->split[p];
        const size_t *in_bounds = side->bounds[from];
        size_t *out_bounds = side->bounds[1 - from];
        for (;;) {
            size_t c = __atomic_fetch_add(&sh->task, 1, __ATOMIC_RELAXED);
            if (c >= clusters) break;
            radix_pass(side->buffer[from], side->buffer[1 - from], in_bounds + c, 1, remaining, sh->split[p], position);
            for (size_t j = 0; j < sub; ++j) out_bounds[c * sub + j] = position[j];
        }
        barrier_wait(&sh->barrier);
        if (id == 0) {
            out_bounds[clusters * sub] = side->rows;
            sh->task = 0;
        }
        clusters *= sub;
        from = 1 - from;
        barrier_wait(&sh->barrier);
    }
    if (id == 0) {
        side->result = side->buffer[from];
        side->result_bounds = side->bounds[from];
    }
}

/* ---------- thread body ------------------------------------------------- */

static void *worker(void *argument) {
    Worker *w = argument;
    Shared *sh = w->shared;
    const int id = w->id, T = sh->threads;
    pin_to_cpu(id);
    JoinResult result = {0, 0};
    barrier_wait(&sh->barrier);
    if (id == 0) sh->stamp[0] = now_ns();

    if (!sh->radix) {
        /* Build: clear my share of buckets, wait, then insert my share of R. */
        size_t b0 = share_begin(sh->bucket_count, id, T), b1 = share_begin(sh->bucket_count, id + 1, T);
        for (size_t i = b0; i < b1; ++i) sh->buckets[i] = EMPTY;
        barrier_wait(&sh->barrier);
        const size_t mask = sh->bucket_count - 1;
        size_t r0 = share_begin(sh->r.rows, id, T), r1 = share_begin(sh->r.rows, id + 1, T);
        if (insert_cas_release) {
            for (size_t i = r0; i < r1; ++i) { /* v2 */
                uint32_t *head = &sh->buckets[mix(sh->r.input[i].key) & mask];
                uint32_t old = __atomic_load_n(head, __ATOMIC_RELAXED);
                do sh->next[i] = old;
                while (!__atomic_compare_exchange_n(head, &old, (uint32_t)i, 1, __ATOMIC_RELEASE, __ATOMIC_RELAXED));
            }
        } else {
            for (size_t i = r0; i < r1; ++i)
                sh->next[i] = __atomic_exchange_n(&sh->buckets[mix(sh->r.input[i].key) & mask], (uint32_t)i,
                                                  __ATOMIC_RELAXED);
        }
        phase_end(sh, id, 1);
        HashTable table = {sh->r.input, sh->buckets, sh->next, mask, 0};
        size_t s0 = share_begin(sh->s.rows, id, T), s1 = share_begin(sh->s.rows, id + 1, T);
        hash_probe(&table, sh->s.input + s0, s1 - s0, &result);
        phase_end(sh, id, 2);
    } else {
        partition_side(sh, &sh->r, id);
        phase_end(sh, id, 1);
        partition_side(sh, &sh->s, id);
        phase_end(sh, id, 2);
        const size_t partitions = (size_t)1 << sh->bits;
        const size_t *rb = sh->r.result_bounds, *sb = sh->s.result_bounds;
        /* Thread 0 lays out every small table in one array (part of build time). */
        if (id == 0) {
            sh->bucket_offset[0] = 0;
            for (size_t p = 0; p < partitions; ++p)
                sh->bucket_offset[p + 1] = sh->bucket_offset[p] + bucket_count_for(rb[p + 1] - rb[p]);
            sh->task = 0;
        }
        barrier_wait(&sh->barrier);
        for (;;) {
            size_t first = __atomic_fetch_add(&sh->task, BATCH, __ATOMIC_RELAXED);
            if (first >= partitions) break;
            for (size_t p = first; p < first + BATCH && p < partitions; ++p) {
                HashTable table;
                hash_build(&table, sh->r.result + rb[p], rb[p + 1] - rb[p], sh->buckets + sh->bucket_offset[p],
                           sh->next + rb[p], sh->bits);
            }
        }
        barrier_wait(&sh->barrier);
        if (id == 0) { sh->stamp[3] = now_ns(); sh->task = 0; }
        barrier_wait(&sh->barrier);
        for (;;) {
            size_t first = __atomic_fetch_add(&sh->task, BATCH, __ATOMIC_RELAXED);
            if (first >= partitions) break;
            for (size_t p = first; p < first + BATCH && p < partitions; ++p) {
                HashTable table = {sh->r.result + rb[p], sh->buckets + sh->bucket_offset[p], sh->next + rb[p],
                                   bucket_count_for(rb[p + 1] - rb[p]) - 1, sh->bits};
                hash_probe(&table, sh->s.result + sb[p], sb[p + 1] - sb[p], &result);
            }
        }
        phase_end(sh, id, 4);
    }
    sh->result[id] = result;
    return NULL;
}

/* ---------- setup shared by the test and the benchmark ------------------ */

typedef struct {
    double phase_ms[4]; /* simple: build, probe; radix: partition R, partition S, build, probe */
    double total_ms;
    JoinResult result;
} RunTimes;

typedef struct {
    Shared sh;
    size_t max_rows, max_bits;
} Context;

static void context_init(Context *c, size_t max_rows, unsigned max_bits) {
    memset(c, 0, sizeof(*c));
    size_t rows = max_rows ? max_rows : 1;
    c->max_rows = rows;
    c->max_bits = max_bits;
    Shared *sh = &c->sh;
    Side *sides[2] = {&sh->r, &sh->s};
    for (int k = 0; k < 2; ++k) {
        for (int i = 0; i < 2; ++i) {
            sides[k]->buffer[i] = allocate_touched(rows * sizeof(Record), PAGES_DEFAULT);
            sides[k]->bounds[i] = allocate_touched((((size_t)1 << max_bits) + 1) * sizeof(size_t), PAGES_DEFAULT);
        }
    }
    sh->histogram = allocate_touched(MAX_THREADS * ((size_t)1 << max_bits) * sizeof(size_t), PAGES_DEFAULT);
    sh->scratch = allocate_touched(MAX_THREADS * (((size_t)1 << max_bits) + 1) * sizeof(size_t), PAGES_DEFAULT);
    /* Enough for the simple table (bucket_count_for(rows)) or all small tables
     * (each at most 4 x its rows + 1). */
    size_t arena = 4 * rows + ((size_t)1 << max_bits) + 2;
    if (bucket_count_for(rows) > arena) arena = bucket_count_for(rows);
    sh->buckets = allocate_touched(arena * sizeof(uint32_t), PAGES_DEFAULT);
    sh->bucket_offset = allocate_touched((((size_t)1 << max_bits) + 1) * sizeof(size_t), PAGES_DEFAULT);
    sh->next = allocate_touched(rows * sizeof(uint32_t), PAGES_DEFAULT);
}

static void context_free(Context *c) {
    Shared *sh = &c->sh;
    Side *sides[2] = {&sh->r, &sh->s};
    for (int k = 0; k < 2; ++k)
        for (int i = 0; i < 2; ++i) { free(sides[k]->buffer[i]); free(sides[k]->bounds[i]); }
    free(sh->histogram); free(sh->scratch); free(sh->buckets); free(sh->bucket_offset); free(sh->next);
}

static RunTimes run_join(Context *c, const Record *left, size_t left_rows, const Record *right, size_t right_rows,
                         int threads, unsigned bits, unsigned passes) {
    Shared *sh = &c->sh;
    if (left_rows > c->max_rows || right_rows > c->max_rows || bits > c->max_bits || threads > MAX_THREADS)
        die("run_join: setting larger than the allocated buffers");
    sh->threads = threads;
    sh->radix = bits > 0;
    sh->bits = bits;
    sh->passes = bits ? passes : 0;
    if (bits) split_bits(bits, passes, sh->split);
    sh->r.input = left; sh->r.rows = left_rows;
    sh->s.input = right; sh->s.rows = right_rows;
    sh->bucket_count = bucket_count_for(left_rows);
    sh->task = 0;
    barrier_init(&sh->barrier, threads);
    pthread_t handle[MAX_THREADS];
    Worker workers[MAX_THREADS];
    for (int t = 0; t < threads; ++t) {
        workers[t] = (Worker){sh, t};
        if (pthread_create(&handle[t], NULL, worker, &workers[t])) die("pthread_create failed");
    }
    for (int t = 0; t < threads; ++t) pthread_join(handle[t], NULL);
    barrier_destroy(&sh->barrier);
    RunTimes rt;
    memset(&rt, 0, sizeof(rt));
    int phases = bits ? 4 : 2;
    for (int i = 0; i < phases; ++i) rt.phase_ms[i] = (sh->stamp[i + 1] - sh->stamp[i]) / 1e6;
    rt.total_ms = (sh->stamp[phases] - sh->stamp[0]) / 1e6;
    for (int t = 0; t < threads; ++t) {
        rt.result.matches += sh->result[t].matches;
        rt.result.checksum += sh->result[t].checksum;
    }
    return rt;
}

/* ---------- correctness ------------------------------------------------- */

static JoinResult nested_loop(const Record *left, size_t ln, const Record *right, size_t rn) {
    JoinResult result = {0, 0};
    for (size_t j = 0; j < rn; ++j)
        for (size_t i = 0; i < ln; ++i)
            if (left[i].key == right[j].key) { ++result.matches; result.checksum += pair_hash(left[i].row_id, right[j].row_id); }
    return result;
}

static void self_test(void) {
    Context c;
    context_init(&c, 4000, 8);
    uint64_t state = 5;
    size_t cases = 0;
    for (size_t trial = 0; trial < 60; ++trial) {
        size_t ln = random_next(&state) % 400, rn = random_next(&state) % 400;
        if (trial == 0) ln = 0;
        if (trial == 1) rn = 0;
        uint32_t range = 1 + (uint32_t)(random_next(&state) % 150);
        Record *a = allocate(ln ? ln : 1, sizeof(Record)), *b = allocate(rn ? rn : 1, sizeof(Record));
        for (size_t i = 0; i < ln; ++i) a[i] = (Record){i % 9 ? (uint32_t)(random_next(&state) % range) : UINT32_MAX, (uint32_t)i};
        for (size_t i = 0; i < rn; ++i) b[i] = (Record){i % 9 ? (uint32_t)(random_next(&state) % range) : UINT32_MAX, (uint32_t)i};
        JoinResult want = nested_loop(a, ln, b, rn);
        for (int threads = 1; threads <= 4; ++threads) {
            for (unsigned passes = 1; passes <= 3; ++passes) {
                for (unsigned bits = passes == 1 ? 0 : passes; bits <= 8; bits += 1) {
                    for (int mode = 0; mode < (bits ? 1 : 2); ++mode) { /* simple join: both insert modes */
                        insert_cas_release = mode;
                        RunTimes rt = run_join(&c, a, ln, b, rn, threads, bits, passes);
                        if (rt.result.matches != want.matches || rt.result.checksum != want.checksum) {
                            fprintf(stderr, "FAIL threads=%d bits=%u passes=%u insert=%d trial=%zu\n", threads, bits,
                                    passes, mode, trial);
                            exit(EXIT_FAILURE);
                        }
                        ++cases;
                    }
                    insert_cas_release = 0;
                }
            }
        }
        free(a); free(b);
    }
    size_t n = 4000;
    Record *l = allocate(n, sizeof(Record)), *r = allocate(n, sizeof(Record));
    generate(l, r, n, 42);
    JoinResult want = expected_result(n);
    for (int threads = 1; threads <= 4; ++threads) {
        RunTimes rt = run_join(&c, l, n, r, n, threads, 6, 2);
        if (rt.result.matches != want.matches || rt.result.checksum != want.checksum) die("FAIL generated data");
        ++cases;
    }
    free(l); free(r);
    context_free(&c);
    printf("Passed %zu parallel join checks against the nested-loop reference "
           "(1 to 4 threads, simple join with both insert methods, radix join with 1 to 8 bits in 1 to 3 passes).\n", cases);
}

/* ---------- benchmark --------------------------------------------------- */

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--test")) { self_test(); return 0; }
    if (argc < 2 || argv[1][0] == '-') {
        fprintf(stderr, "Usage: %s --test\n       %s OUTPUT_DIRECTORY [--rows N] [--threads 1,2,4] "
                "[--bits B] [--passes P] [--repeats R] [--seed S] [--insert swap|cas-release]\n", argv[0], argv[0]);
        return EXIT_FAILURE;
    }
    size_t rows = 16000000, bits = 14, passes = 1, repeats = 15, seed = 42;
    size_t thread_list[8] = {1, 2, 4}, thread_count = 3;
    for (int i = 2; i < argc; i += 2) {
        if (i + 1 >= argc) die("Missing value after option");
        if (!strcmp(argv[i], "--rows")) rows = parse_number(argv[i + 1], 1000, (size_t)1 << 30);
        else if (!strcmp(argv[i], "--threads")) thread_count = parse_list(argv[i + 1], thread_list, 8, 1, MAX_THREADS);
        else if (!strcmp(argv[i], "--bits")) bits = parse_number(argv[i + 1], 1, 22);
        else if (!strcmp(argv[i], "--passes")) passes = parse_number(argv[i + 1], 1, MAX_PASSES);
        else if (!strcmp(argv[i], "--repeats")) repeats = parse_number(argv[i + 1], 1, 100);
        else if (!strcmp(argv[i], "--seed")) seed = parse_number(argv[i + 1], 0, UINT32_MAX);
        else if (!strcmp(argv[i], "--insert")) {
            if (!strcmp(argv[i + 1], "cas-release")) insert_cas_release = 1;
            else if (strcmp(argv[i + 1], "swap")) die("--insert must be swap or cas-release");
        }
        else die("Unknown option");
    }
    if (passes > bits) die("--passes cannot be larger than --bits");
    make_results_directory(argv[1]);
    FILE *env = open_in(argv[1], "environment.txt");
    write_common_environment(env);
    fprintf(env, "experiment=parallel_join\nrows=%zu\nradix_bits=%zu\nradix_passes=%zu\nrepeats=%zu\nwarmup_runs=1\nseed=%zu\n"
            "run_order=shuffled every round\n",
            rows, bits, passes, repeats, seed);
    fputs(insert_cas_release ? "simple_insert=cas-release (v2: compare-and-swap that first finishes earlier writes)\n"
                             : "simple_insert=swap (one atomic swap per insert, no ordering request)\n", env);
#ifndef __APPLE__  /* M5 copy: on macOS write_common_environment already says that pinning is not supported */
    fputs("thread_pinning=thread i on the i-th allowed CPU, wrapping around if there are fewer CPUs\n", env);
#endif
    fclose(env);
    FILE *out = open_in(argv[1], "timings.csv");
    fputs("rows_per_input,algorithm,threads,total_bits,passes,run,position,steal_ms,phase,ms\n", out);

    Record *left = allocate_touched(rows * sizeof(Record), PAGES_DEFAULT);
    Record *right = allocate_touched(rows * sizeof(Record), PAGES_DEFAULT);
    generate(left, right, rows, seed);
    JoinResult expected = expected_result(rows);
    Context c;
    context_init(&c, rows, (unsigned)bits);
    static const char *simple_phase[] = {"build", "probe"};
    static const char *radix_phase[] = {"partition_r", "partition_s", "build", "probe"};
    size_t order[2 * 8], combos = 2 * thread_count;
    uint64_t order_state = 3000;
    for (size_t run = 0; run <= repeats; ++run) { /* run 0 is a verified warmup */
        shuffle_order(order, combos, &order_state);
        for (size_t position = 0; position < combos; ++position) {
            int threads = (int)thread_list[order[position] / 2];
            int radix = (int)(order[position] % 2);
            unsigned b = radix ? (unsigned)bits : 0;
            double steal_before = steal_ms(-1);
            RunTimes rt = run_join(&c, left, rows, right, rows, threads, b, (unsigned)passes);
            double stolen = steal_delta(steal_before, steal_ms(-1));
            if (rt.result.matches != expected.matches || rt.result.checksum != expected.checksum)
                die("Join result does not match the expected answer");
            if (!run) continue;
            const char *name = radix ? "radix" : "simple";
            int phases = radix ? 4 : 2;
            for (int ph = 0; ph <= phases; ++ph)
                fprintf(out, "%zu,%s,%d,%u,%zu,%zu,%zu,%.1f,%s,%.6f\n", rows, name, threads, b, radix ? passes : 0,
                        run, position, stolen, ph == phases ? "total" : radix ? radix_phase[ph] : simple_phase[ph],
                        ph == phases ? rt.total_ms : rt.phase_ms[ph]);
        }
        fflush(out);
        printf("%s %zu of %zu\n", run ? "recorded run" : "verified warmup", run, repeats);
        fflush(stdout);
    }
    fclose(out);
    context_free(&c);
    free(left); free(right);
    return 0;
}
