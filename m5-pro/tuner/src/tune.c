/* Radix hash join tuner, spec version 1 (frozen; see ../SPEC.md).
 *
 * Finds the fastest settings of the radix hash join on the machine it runs
 * on, by timing the join. It never reads cache, TLB or memory sizes: the
 * timings already contain their effect.
 *
 * Every frozen item of the spec is a #define, a table or a fixed function in
 * this file. The search changes only the three knobs in `Knobs`.
 *
 * Build:  make
 * Test:   ./bin/tune --test
 * Run:    ./bin/tune OUTPUT_DIR [--quick]
 *
 * Standalone: shares no code with the rest of m5-pro/. The join is a copy of
 * experiment 4's radix join (Boncz, Manegold and Kersten), without any clock
 * reads inside the timed part.
 */
#if defined(__linux__)
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <pthread.h>
#include <sys/qos.h>
#include <sys/sysctl.h>
#endif
#ifdef __linux__
#include <sched.h>
#endif

/* ---------- frozen spec, version 1 ------------------------------------- */

#define SPEC_VERSION 1
#define SEED 42
#define MAX_BITS 18             /* radix bits: 0 .. min(MAX_BITS, log2(rows) - 4) */
#define MIN_PARTITION_LOG2 4    /* so the smallest partitions still hold about 16 rows */
#define MAX_PASSES 3
static const double LOADS[] = {0.125, 0.25, 0.5, 1.0, 2.0, 4.0}; /* rows per bucket */
#define N_LOADS ((int)(sizeof LOADS / sizeof LOADS[0]))
#define CURVE_LOAD 0.5          /* rows per bucket while the stage A curve is drawn */
#define MEMORY_SHARE 4          /* a size is skipped if it needs more than 1/4 of the RAM */

typedef struct {
    const char *name;
    size_t train[3];
    int n_train;
    size_t test;
    int rounds_a, rounds_b, rounds_c, rounds_t;
    int top_b, top_c, span_t;
} Profile;

/* The official profile. "quick" only checks that everything runs. */
static const Profile FULL = {"full", {(size_t)1 << 20, (size_t)1 << 22, (size_t)1 << 24}, 3, (size_t)1 << 23,
                             3, 3, 11, 5, 4, 5, 3};
static const Profile QUICK = {"quick", {(size_t)1 << 16, (size_t)1 << 18, 0}, 2, (size_t)1 << 17,
                              1, 1, 3, 3, 3, 3, 2};

/* ---------- records, hash, clock, memory -------------------------------- */

#define EMPTY UINT32_MAX
#define MAX_CANDS 128
#define MAX_ROUNDS 16

typedef struct {
    uint32_t key; /* join key */
    uint32_t row; /* row number in its table */
} Rec;

typedef struct {
    uint64_t matches, checksum;
} Result;

typedef struct {
    unsigned bits;   /* radix bits; 0 = no partitioning (one hash table on all of R) */
    unsigned passes; /* partitioning passes; 0 when bits == 0 */
    double load;     /* rows per bucket in each partition's hash table */
} Knobs;

static void die(const char *message) {
    fprintf(stderr, "tune: %s\n", message);
    exit(EXIT_FAILURE);
}

/* SplitMix64 finalizer, the hash of every experiment. */
static inline uint64_t mix(uint64_t v) {
    v ^= v >> 30;
    v *= UINT64_C(0xbf58476d1ce4e5b9);
    v ^= v >> 27;
    v *= UINT64_C(0x94d049bb133111eb);
    return v ^ (v >> 31);
}

static inline uint64_t pair_hash(uint32_t left_row, uint32_t right_row) {
    return mix(((uint64_t)left_row << 32) | right_row);
}

static inline uint64_t now_ns(void) {
#ifdef __APPLE__
    return clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
#else
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
#endif
}

/* Allocated and written once, so the operating system sets up every page
 * before any timing starts. */
static void *touched(size_t bytes) {
    void *p = NULL;
    if (posix_memalign(&p, 16384, bytes ? bytes : 1)) die("out of memory");
    memset(p, 0, bytes ? bytes : 1);
    return p;
}

static uint64_t rng_next(uint64_t *state) {
    *state += UINT64_C(0x9e3779b97f4a7c15);
    return mix(*state);
}

static size_t pow2_at_least(size_t x) {
    size_t c = 1;
    while (c < x) c <<= 1;
    return c;
}

static unsigned log2_floor(size_t x) {
    unsigned b = 0;
    while (x >>= 1) ++b;
    return b;
}

static unsigned max_bits_for(size_t rows) {
    unsigned b = log2_floor(rows);
    b = b > MIN_PARTITION_LOG2 ? b - MIN_PARTITION_LOG2 : 0;
    return b < MAX_BITS ? b : MAX_BITS;
}

/* Smallest power of two that keeps the average at or below `load` rows per bucket. */
static inline size_t buckets_for(size_t rows, double load) {
    size_t want = (size_t)ceil((double)rows / load);
    return pow2_at_least(want ? want : 1);
}

/* ---------- data (same generator as experiments 2 to 5) ----------------- */

/* R keys 0..N-1, S keys 0,2,..,2(N-1), both shuffled: exactly ceil(N/2) matches. */
static void generate(Rec *r, Rec *s, size_t n, uint64_t seed) {
    for (size_t i = 0; i < n; ++i) {
        r[i] = (Rec){(uint32_t)i, (uint32_t)i};
        s[i] = (Rec){(uint32_t)(2 * i), (uint32_t)i};
    }
    for (size_t i = n; i > 1; --i) {
        size_t j = rng_next(&seed) % i;
        Rec t = r[i - 1]; r[i - 1] = r[j]; r[j] = t;
    }
    for (size_t i = n; i > 1; --i) {
        size_t j = rng_next(&seed) % i;
        Rec t = s[i - 1]; s[i - 1] = s[j]; s[j] = t;
    }
}

static Result expected_result(size_t n) {
    Result e = {(n + 1) / 2, 0};
    for (size_t i = 0; i < n; i += 2) e.checksum += pair_hash((uint32_t)i, (uint32_t)(i / 2));
    return e;
}

/* ---------- the radix hash join ------------------------------------------ */

typedef struct {
    size_t n;
    Rec *r, *s;              /* inputs */
    Rec *ra, *rb, *sa, *sb;  /* partitioning work buffers, two per input */
    size_t *bounds[4];       /* partition bounds, two per input */
    uint32_t *buckets, *next;
    Result expected;
} Work;

/* One partitioning pass over every existing cluster (copy of experiment 4's
 * radix_pass without its clock reads). Cluster c is src[start[c] ..
 * start[c+1]); it is split on `bits` hash bits starting at bit `shift`.
 * out gets clusters * 2^bits + 1 bounds. */
static void radix_pass(const Rec *src, Rec *dst, const size_t *start, size_t clusters,
                       unsigned shift, unsigned bits, size_t *out) {
    const size_t fan = (size_t)1 << bits, total = clusters * fan, mask = fan - 1;
    memset(out, 0, (total + 1) * sizeof *out);
    out[0] = start[0];
    for (size_t c = 0; c < clusters; ++c) {
        size_t *count = out + c * fan + 1;
        for (size_t i = start[c]; i < start[c + 1]; ++i) ++count[(mix(src[i].key) >> shift) & mask];
    }
    for (size_t i = 1; i <= total; ++i) out[i] += out[i - 1];
    for (size_t c = 0; c < clusters; ++c) {
        size_t *pos = out + c * fan;
        for (size_t i = start[c]; i < start[c + 1]; ++i) dst[pos[(mix(src[i].key) >> shift) & mask]++] = src[i];
    }
    for (size_t i = total; i > 0; --i) out[i] = out[i - 1];
    out[0] = start[0];
}

/* All passes. Bits are split as evenly as possible; earlier passes get the
 * extra bit; pass 1 splits on the highest group of the `bits` low hash bits,
 * so final cluster p holds exactly the rows whose low `bits` hash bits are p. */
static const Rec *partition(const Rec *in, size_t n, unsigned bits, unsigned passes, Rec *a, Rec *b,
                            size_t *bound_a, size_t *bound_b, const size_t **bounds) {
    size_t whole[2] = {0, n};
    const Rec *src = in;
    Rec *dst = a;
    const size_t *start = whole;
    size_t *out = bound_a, clusters = 1;
    unsigned remaining = bits;
    for (unsigned p = 0; p < passes; ++p) {
        unsigned these = bits / passes + (p < bits % passes);
        remaining -= these;
        radix_pass(src, dst, start, clusters, remaining, these, out);
        clusters <<= these;
        start = out;
        out = out == bound_a ? bound_b : bound_a;
        src = dst;
        dst = dst == a ? b : a;
    }
    *bounds = start;
    return src;
}

/* Build a chained hash table on r (buckets from the hash bits above the radix
 * bits), then probe it with every row of s. Clearing the buckets is counted. */
static inline void build_probe(const Rec *r, size_t rn, const Rec *s, size_t sn, unsigned shift, double load,
                               uint32_t *buckets, uint32_t *next, Result *res) {
    const size_t nb = buckets_for(rn, load), mask = nb - 1;
    for (size_t i = 0; i < nb; ++i) buckets[i] = EMPTY;
    for (size_t i = 0; i < rn; ++i) {
        size_t b = (mix(r[i].key) >> shift) & mask;
        next[i] = buckets[b];
        buckets[b] = (uint32_t)i;
    }
    for (size_t i = 0; i < sn; ++i) {
        uint32_t key = s[i].key;
        for (uint32_t x = buckets[(mix(key) >> shift) & mask]; x != EMPTY; x = next[x]) {
            if (r[x].key == key) {
                ++res->matches;
                res->checksum += pair_hash(r[x].row, s[i].row);
            }
        }
    }
}

static Result run_join(Work *w, Knobs k) {
    Result res = {0, 0};
    if (k.bits == 0) {
        build_probe(w->r, w->n, w->s, w->n, 0, k.load, w->buckets, w->next, &res);
        return res;
    }
    const size_t *rb, *sb;
    const Rec *r = partition(w->r, w->n, k.bits, k.passes, w->ra, w->rb, w->bounds[0], w->bounds[1], &rb);
    const Rec *s = partition(w->s, w->n, k.bits, k.passes, w->sa, w->sb, w->bounds[2], w->bounds[3], &sb);
    const size_t parts = (size_t)1 << k.bits;
    for (size_t p = 0; p < parts; ++p)
        build_probe(r + rb[p], rb[p + 1] - rb[p], s + sb[p], sb[p + 1] - sb[p], k.bits, k.load,
                    w->buckets, w->next, &res);
    return res;
}

static size_t bytes_needed(size_t n) {
    return 6 * n * sizeof(Rec) + n * sizeof(uint32_t) + buckets_for(n, LOADS[0]) * sizeof(uint32_t)
           + 4 * (((size_t)1 << MAX_BITS) + 1) * sizeof(size_t);
}

static Work work_new(size_t n) {
    Work w;
    memset(&w, 0, sizeof w);
    w.n = n;
    w.r = touched(n * sizeof(Rec));
    w.s = touched(n * sizeof(Rec));
    w.ra = touched(n * sizeof(Rec));
    w.rb = touched(n * sizeof(Rec));
    w.sa = touched(n * sizeof(Rec));
    w.sb = touched(n * sizeof(Rec));
    for (int i = 0; i < 4; ++i) w.bounds[i] = touched((((size_t)1 << MAX_BITS) + 1) * sizeof(size_t));
    w.buckets = touched(buckets_for(n, LOADS[0]) * sizeof(uint32_t));
    w.next = touched(n * sizeof(uint32_t));
    return w;
}

static void work_free(Work *w) {
    void *all[] = {w->r, w->s, w->ra, w->rb, w->sa, w->sb, w->bounds[0], w->bounds[1], w->bounds[2],
                   w->bounds[3], w->buckets, w->next};
    for (size_t i = 0; i < sizeof all / sizeof all[0]; ++i) free(all[i]);
}

/* One timed join. A wrong answer stops the tuner: it must never pick a setting that breaks the join. */
static double measure(Work *w, Knobs k) {
    uint64_t t0 = now_ns();
    Result r = run_join(w, k);
    uint64_t t1 = now_ns();
    if (r.matches != w->expected.matches || r.checksum != w->expected.checksum) {
        fprintf(stderr, "tune: wrong answer at %zu rows, %u bits, %u passes, %g rows per bucket\n",
                w->n, k.bits, k.passes, k.load);
        exit(EXIT_FAILURE);
    }
    return (double)(t1 - t0) / (double)w->n;
}

/* ---------- labels --------------------------------------------------------- */

static const char *load_text(double load, char *buf, size_t len) {
    if (load < 1) snprintf(buf, len, "1/%d", (int)lround(1 / load));
    else snprintf(buf, len, "%g", load);
    return buf;
}

static const char *knob_text(Knobs k, char *buf, size_t len) {
    char l[16];
    if (k.bits == 0) snprintf(buf, len, "no partitioning, %s rows/bucket", load_text(k.load, l, sizeof l));
    else snprintf(buf, len, "%u bits, %u pass%s, %s rows/bucket", k.bits, k.passes, k.passes > 1 ? "es" : "",
                  load_text(k.load, l, sizeof l));
    return buf;
}

static const char *short_knobs(Knobs k, char *buf, size_t len) {
    snprintf(buf, len, "%u/%u/%g", k.bits, k.passes, k.load);
    return buf;
}

static int same_knobs(Knobs a, Knobs b) {
    return a.bits == b.bits && a.passes == b.passes && fabs(a.load - b.load) < 1e-9;
}

/* ---------- measurement stages ------------------------------------------- */

typedef struct {
    Knobs k;
    double loss[MAX_ROUNDS];
    int runs;
    double median, min, max;
} Cand;

static FILE *trials_file, *stages_file;
static uint64_t order_state = SEED;

static int cmp_double(const void *a, const void *b) {
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static int cmp_median(const void *a, const void *b) {
    const Cand *x = a, *y = b;
    return (x->median > y->median) - (x->median < y->median);
}

static void stats(Cand *c) {
    double t[MAX_ROUNDS];
    memcpy(t, c->loss, sizeof(double) * (size_t)c->runs);
    qsort(t, (size_t)c->runs, sizeof(double), cmp_double);
    c->median = c->runs % 2 ? t[c->runs / 2] : (t[c->runs / 2 - 1] + t[c->runs / 2]) / 2;
    c->min = t[0];
    c->max = t[c->runs - 1];
}

/* Every round runs every setting once, in a newly shuffled order, so a slow
 * moment of the machine hits all settings alike. */
static double run_stage(Work *w, const char *stage, Cand *c, int nc, int rounds) {
    int order[MAX_CANDS];
    uint64_t t0 = now_ns();
    for (int r = 0; r < rounds; ++r) {
        for (int i = 0; i < nc; ++i) order[i] = i;
        for (int i = nc; i > 1; --i) {
            int j = (int)(rng_next(&order_state) % (uint64_t)i);
            int t = order[i - 1]; order[i - 1] = order[j]; order[j] = t;
        }
        for (int pos = 0; pos < nc; ++pos) {
            Cand *x = &c[order[pos]];
            double loss = measure(w, x->k);
            x->loss[x->runs++] = loss;
            fprintf(trials_file, "%d,%zu,%s,%d,%d,%u,%u,%g,%.4f\n", SPEC_VERSION, w->n, stage, r + 1, pos + 1,
                    x->k.bits, x->k.passes, x->k.load, loss);
        }
    }
    for (int i = 0; i < nc; ++i) {
        stats(&c[i]);
        fprintf(stages_file, "%d,%zu,%s,%u,%u,%g,%d,%.4f,%.4f,%.4f\n", SPEC_VERSION, w->n, stage, c[i].k.bits,
                c[i].k.passes, c[i].k.load, c[i].runs, c[i].median, c[i].min, c[i].max);
    }
    fflush(trials_file);
    fflush(stages_file);
    return (double)(now_ns() - t0) / 1e9;
}

static int best_index(const Cand *c, int nc) {
    int b = 0;
    for (int i = 1; i < nc; ++i) if (c[i].median < c[b].median) b = i;
    return b;
}

/* Stage A's loss curve in the terminal: one row per number of radix bits. */
static void print_curve(const Cand *a, int na, unsigned bmax, size_t n) {
    double med[MAX_BITS + 1][MAX_PASSES + 1];
    for (unsigned b = 0; b <= MAX_BITS; ++b) for (unsigned p = 0; p <= MAX_PASSES; ++p) med[b][p] = NAN;
    for (int i = 0; i < na; ++i) med[a[i].k.bits][a[i].k.passes] = a[i].median;
    double row_min[MAX_BITS + 1], top = 0, low = INFINITY;
    unsigned low_b = 0;
    for (unsigned b = 0; b <= bmax; ++b) {
        row_min[b] = INFINITY;
        for (unsigned p = 0; p <= MAX_PASSES; ++p) if (!isnan(med[b][p]) && med[b][p] < row_min[b]) row_min[b] = med[b][p];
        if (row_min[b] > top) top = row_min[b];
        if (row_min[b] < low) { low = row_min[b]; low_b = b; }
    }
    printf("\n  Loss curve at %zu rows per input: ns per input row, median, 1/2 row per bucket\n", n);
    printf("  bits    1 pass  2 passes  3 passes\n");
    for (unsigned b = 0; b <= bmax; ++b) {
        printf("  %4u", b);
        if (b == 0) printf("  %8.2f  %8s  %8s ", med[0][0], "", "");
        else for (unsigned p = 1; p <= MAX_PASSES; ++p) {
            if (isnan(med[b][p])) printf("  %8s", "-");
            else printf("  %8.2f", med[b][p]);
        }
        printf("  ");
        int width = (int)lround(32.0 * row_min[b] / top);
        for (int i = 0; i < width; ++i) fputs("\xe2\x96\x88", stdout); /* full block */
        if (b == 0) printf(" no partitioning");
        if (b == low_b) printf(" <- lowest");
        printf("\n");
    }
}

/* ---------- machine description (recorded, never used by the search) ----- */

static void cpu_name(char *buf, size_t len) {
    snprintf(buf, len, "unknown CPU");
#ifdef __APPLE__
    size_t l = len;
    if (sysctlbyname("machdep.cpu.brand_string", buf, &l, NULL, 0) == 0) return;
#endif
#ifdef __linux__
    FILE *f = fopen("/proc/cpuinfo", "r");
    if (f) {
        char line[512];
        while (fgets(line, sizeof line, f)) {
            if (!strncmp(line, "model name", 10) || !strncmp(line, "Model", 5)) {
                char *v = strchr(line, ':');
                if (v) {
                    v += 1 + (v[1] == ' ');
                    v[strcspn(v, "\n")] = 0;
                    snprintf(buf, len, "%s", v);
                    fclose(f);
                    return;
                }
            }
        }
        fclose(f);
    }
#endif
    struct utsname u;
    if (uname(&u) == 0) snprintf(buf, len, "%s CPU", u.machine);
}

static void machine_tag(char *buf, size_t len) {
    char name[256];
    cpu_name(name, sizeof name);
    size_t j = 0;
    int dash = 0;
    for (size_t i = 0; name[i] && j + 1 < len && j < 40; ++i) {
        char c = name[i];
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) { buf[j++] = c; dash = 0; }
        else if (c >= 'A' && c <= 'Z') { buf[j++] = (char)(c - 'A' + 'a'); dash = 0; }
        else if (!dash && j > 0) { buf[j++] = '-'; dash = 1; }
    }
    while (j > 0 && buf[j - 1] == '-') --j;
    buf[j] = 0;
    if (!j) snprintf(buf, len, "machine");
}

static size_t ram_bytes(void) {
    long pages = sysconf(_SC_PHYS_PAGES), size = sysconf(_SC_PAGESIZE);
    return pages > 0 && size > 0 ? (size_t)pages * (size_t)size : 0;
}

static const char *set_priority(char *buf, size_t len) {
#ifdef __APPLE__
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
    snprintf(buf, len, "macOS cannot pin threads; highest QoS class (prefers performance cores)");
#elif defined(__linux__)
    int cpu = sched_getcpu();
    cpu_set_t set;
    CPU_ZERO(&set);
    if (cpu >= 0) CPU_SET(cpu, &set);
    if (cpu >= 0 && sched_setaffinity(0, sizeof set, &set) == 0) snprintf(buf, len, "pinned to CPU %d", cpu);
    else snprintf(buf, len, "not pinned (sched_setaffinity failed)");
#else
    snprintf(buf, len, "default priority");
#endif
    return buf;
}

/* ---------- self-test ------------------------------------------------------ */

static size_t test_count = 0;

static void expect(int ok, const char *what) {
    if (!ok) {
        fprintf(stderr, "tune --test FAILED: %s\n", what);
        exit(EXIT_FAILURE);
    }
    ++test_count;
}

static Result nested_loop(const Rec *r, size_t rn, const Rec *s, size_t sn) {
    Result e = {0, 0};
    for (size_t i = 0; i < rn; ++i)
        for (size_t j = 0; j < sn; ++j)
            if (r[i].key == s[j].key) { ++e.matches; e.checksum += pair_hash(r[i].row, s[j].row); }
    return e;
}

static void check_all_knobs(Work *w, const char *what) {
    char msg[256];
    for (int li = 0; li < N_LOADS; ++li) {
        Knobs k = {0, 0, LOADS[li]};
        Result got = run_join(w, k);
        snprintf(msg, sizeof msg, "%s, %zu rows, no partitioning, load %g", what, w->n, k.load);
        expect(got.matches == w->expected.matches && got.checksum == w->expected.checksum, msg);
        for (unsigned bits = 1; bits <= 8; ++bits) {
            for (unsigned passes = 1; passes <= MAX_PASSES && passes <= bits; ++passes) {
                k = (Knobs){bits, passes, LOADS[li]};
                got = run_join(w, k);
                snprintf(msg, sizeof msg, "%s, %zu rows, %u bits, %u passes, load %g", what, w->n, bits, passes,
                         k.load);
                expect(got.matches == w->expected.matches && got.checksum == w->expected.checksum, msg);
            }
        }
    }
}

static void self_test(void) {
    /* 1. Partitioning: every row lands in the partition named by its low hash bits, none lost. */
    const size_t pn = 5000;
    Work w = work_new(pn);
    generate(w.r, w.s, pn, SEED);
    for (unsigned bits = 1; bits <= 12; ++bits) {
        for (unsigned passes = 1; passes <= MAX_PASSES && passes <= bits; ++passes) {
            const size_t *b;
            const Rec *out = partition(w.r, pn, bits, passes, w.ra, w.rb, w.bounds[0], w.bounds[1], &b);
            expect(b[0] == 0 && b[(size_t)1 << bits] == pn, "partition bounds cover every row");
            uint64_t key_sum = 0;
            for (size_t p = 0; p < ((size_t)1 << bits); ++p) {
                expect(b[p] <= b[p + 1], "partition bounds are ordered");
                for (size_t i = b[p]; i < b[p + 1]; ++i) {
                    expect((mix(out[i].key) & (((size_t)1 << bits) - 1)) == p, "row in the partition of its hash bits");
                    key_sum += out[i].key;
                }
            }
            expect(key_sum == (uint64_t)pn * (pn - 1) / 2, "partitioning keeps every key once");
        }
    }
    work_free(&w);
    /* 2. The benchmark data: every knob setting gives the known answer. */
    const size_t sizes[] = {0, 1, 2, 3, 7, 64, 1000, 4097};
    for (size_t i = 0; i < sizeof sizes / sizeof sizes[0]; ++i) {
        w = work_new(sizes[i]);
        generate(w.r, w.s, w.n, SEED);
        w.expected = expected_result(w.n);
        check_all_knobs(&w, "benchmark data");
        work_free(&w);
    }
    /* 3. Duplicate keys on both sides, against a nested loop. */
    const size_t dup_sizes[] = {1, 5, 100, 1500};
    uint64_t state = 7;
    for (size_t i = 0; i < sizeof dup_sizes / sizeof dup_sizes[0]; ++i) {
        size_t n = dup_sizes[i];
        w = work_new(n);
        for (size_t j = 0; j < n; ++j) {
            w.r[j] = (Rec){(uint32_t)(rng_next(&state) % (n / 3 + 1)), (uint32_t)j};
            w.s[j] = (Rec){(uint32_t)(rng_next(&state) % (n / 3 + 1)), (uint32_t)j};
        }
        w.expected = nested_loop(w.r, n, w.s, n);
        check_all_knobs(&w, "duplicate keys");
        work_free(&w);
    }
    /* 4. The rule from bucket counts. */
    expect(buckets_for(1000, 0.5) == 2048 && buckets_for(1000, 4) == 256 && buckets_for(0, 0.5) == 1,
           "bucket counts are the smallest power of two at or below the target load");
    printf("Passed %zu checks (partitioning, %d bucket loads, 0 to 8 bits in 1 to 3 passes, duplicate keys).\n",
           test_count, N_LOADS);
}

/* ---------- the tuning run -------------------------------------------------- */

/* A winner at the edge of a knob's range may hide a better value outside it. */
static void edge_note(Knobs k, unsigned bmax, char *buf, size_t len) {
    buf[0] = 0;
    if (k.bits && k.bits == bmax) snprintf(buf, len, "bits at the largest tested value");
    if (fabs(k.load - LOADS[0]) < 1e-9 || fabs(k.load - LOADS[N_LOADS - 1]) < 1e-9)
        snprintf(buf + strlen(buf), len - strlen(buf), "%srows per bucket at the %s tested value", *buf ? "; " : "",
                 k.load < 1 ? "smallest" : "largest");
}

typedef struct {
    size_t n;
    Knobs win;
    Cand final_win;
    double simple_median;
    char ties[512];
    char edge[128];
    double seconds;
} SizeResult;

static FILE *open_csv(const char *dir, const char *name, const char *header) {
    char path[1024];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "w");
    if (!f) die("cannot write output files");
    fprintf(f, "%s\n", header);
    return f;
}

static SizeResult tune_size(size_t n, const Profile *pf, FILE *best_file) {
    SizeResult res;
    memset(&res, 0, sizeof res);
    res.n = n;
    char a1[96], a2[96];
    uint64_t t0 = now_ns();
    Work w = work_new(n);
    generate(w.r, w.s, n, SEED);
    w.expected = expected_result(n);
    unsigned bmax = max_bits_for(n);
    printf("\n== %zu rows per input (radix bits 0 to %u)\n", n, bmax);
    /* Warm-up, not recorded. */
    measure(&w, (Knobs){0, 0, CURVE_LOAD});
    measure(&w, (Knobs){bmax / 2 ? bmax / 2 : 1, 1, CURVE_LOAD});

    /* Stage A: the loss curve over radix bits and passes. */
    static Cand A[MAX_CANDS], B[MAX_CANDS], C[MAX_CANDS];
    int na = 0, nb = 0, nc = 0;
    A[na++] = (Cand){.k = {0, 0, CURVE_LOAD}};
    for (unsigned bits = 1; bits <= bmax; ++bits)
        for (unsigned p = 1; p <= MAX_PASSES && p <= bits; ++p) A[na++] = (Cand){.k = {bits, p, CURVE_LOAD}};
    double s = run_stage(&w, "A", A, na, pf->rounds_a);
    printf("  stage A, curve:  %d settings x %d rounds, %.1f s\n", na, pf->rounds_a, s);
    print_curve(A, na, bmax, n);
    Knobs a_win = A[best_index(A, na)].k;

    /* Stage B: bucket load for the best few (bits, passes). */
    Cand sorted[MAX_CANDS];
    memcpy(sorted, A, sizeof(Cand) * (size_t)na);
    qsort(sorted, (size_t)na, sizeof(Cand), cmp_median);
    for (int i = 0; i < pf->top_b && i < na; ++i)
        for (int li = 0; li < N_LOADS; ++li)
            B[nb++] = (Cand){.k = {sorted[i].k.bits, sorted[i].k.passes, LOADS[li]}};
    s = run_stage(&w, "B", B, nb, pf->rounds_b);
    printf("\n  stage B, rows per bucket for the best %d:  %d settings x %d rounds, %.1f s\n", pf->top_b, nb,
           pf->rounds_b, s);

    /* Stage C: final race between the best few, plus two references. */
    memcpy(sorted, B, sizeof(Cand) * (size_t)nb);
    qsort(sorted, (size_t)nb, sizeof(Cand), cmp_median);
    Knobs refs[2] = {{0, 0, CURVE_LOAD}, a_win};
    for (int i = 0; i < pf->top_c && i < nb; ++i) C[nc++] = (Cand){.k = sorted[i].k};
    for (int r = 0; r < 2; ++r) {
        int have = 0;
        for (int i = 0; i < nc; ++i) have |= same_knobs(C[i].k, refs[r]);
        if (!have) C[nc++] = (Cand){.k = refs[r]};
    }
    s = run_stage(&w, "C", C, nc, pf->rounds_c);
    printf("  stage C, final race:  %d settings x %d rounds, %.1f s\n", nc, pf->rounds_c, s);
    memcpy(sorted, C, sizeof(Cand) * (size_t)nc);
    qsort(sorted, (size_t)nc, sizeof(Cand), cmp_median);
    const Cand *win = &sorted[0];
    res.win = win->k;
    res.final_win = *win;
    for (int i = 0; i < nc; ++i) {
        const Cand *c = &sorted[i];
        if (same_knobs(c->k, refs[0])) res.simple_median = c->median;
        int tied = i > 0 && c->min <= win->max;
        printf("    %-44s %8.2f ns/row  (%.2f to %.2f)%s\n", knob_text(c->k, a1, sizeof a1), c->median, c->min,
               c->max, i == 0 ? "  <- fastest" : tied ? "  tied" : "");
        if (tied) {
            size_t len = strlen(res.ties);
            snprintf(res.ties + len, sizeof res.ties - len, "%s%s", len ? ";" : "", short_knobs(c->k, a2, sizeof a2));
        }
    }
    edge_note(win->k, bmax, res.edge, sizeof res.edge);
    fprintf(best_file, "%d,%zu,train,%u,%u,%g,%.4f,%.4f,%.4f,%.4f,%.3f,%s,%s\n", SPEC_VERSION, n, win->k.bits,
            win->k.passes, win->k.load, win->median, win->min, win->max, res.simple_median,
            res.simple_median / win->median, res.ties, res.edge);
    fflush(best_file);
    work_free(&w);
    res.seconds = (double)(now_ns() - t0) / 1e9;
    printf("  best: %s, %.2f ns per row, %.2fx as fast as no partitioning (%.0f s for this size)\n",
           knob_text(win->k, a1, sizeof a1), win->median, res.simple_median / win->median, res.seconds);
    if (*res.edge) printf("  note: %s; a value outside the frozen range might be faster.\n", res.edge);
    return res;
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--test")) { self_test(); return 0; }
    if (argc == 2 && !strcmp(argv[1], "--machine-tag")) {
        char tag[64];
        machine_tag(tag, sizeof tag);
        printf("%s\n", tag);
        return 0;
    }
    if (argc < 2 || argv[1][0] == '-' || (argc == 3 && strcmp(argv[2], "--quick")) || argc > 3) {
        fprintf(stderr, "usage: %s OUTPUT_DIR [--quick] | --test | --machine-tag\n", argv[0]);
        return 2;
    }
    const char *dir = argv[1];
    const Profile *pf = argc == 3 ? &QUICK : &FULL;
    {   /* mkdir -p */
        char path[1024];
        snprintf(path, sizeof path, "%s", dir);
        for (char *p = path + 1; *p; ++p)
            if (*p == '/') { *p = 0; if (mkdir(path, 0755) && errno != EEXIST) die("cannot create the output folder"); *p = '/'; }
        if (mkdir(path, 0755) && errno != EEXIST) die("cannot create the output folder");
    }
    uint64_t t_start = now_ns();

    char cpu[256], prio[128], tag[64];
    cpu_name(cpu, sizeof cpu);
    machine_tag(tag, sizeof tag);
    set_priority(prio, sizeof prio);
    size_t ram = ram_bytes();
    struct utsname u;
    uname(&u);

    /* Sizes that fit in a quarter of the RAM. */
    size_t train[3];
    int n_train = 0;
    char dropped[256] = "";
    for (int i = 0; i < pf->n_train; ++i) {
        if (!ram || bytes_needed(pf->train[i]) <= ram / MEMORY_SHARE) train[n_train++] = pf->train[i];
        else snprintf(dropped + strlen(dropped), sizeof dropped - strlen(dropped), "%s%zu", *dropped ? "," : "",
                      pf->train[i]);
    }
    size_t test = !ram || bytes_needed(pf->test) <= ram / MEMORY_SHARE ? pf->test : 0;
    if (!n_train) die("not enough memory for the smallest training size");

    printf("Radix join tuner, spec %d, profile %s, on %s (%s %s)\n", SPEC_VERSION, pf->name, cpu, u.sysname,
           u.release);
    printf("Thread: %s. Cache sizes are not read: the timings decide.\n", prio);
    if (*dropped) printf("Skipped sizes (need more than 1/%d of the RAM): %s\n", MEMORY_SHARE, dropped);

    trials_file = open_csv(dir, "trials.csv", "spec,rows_per_input,stage,round,position,bits,passes,rows_per_bucket,ns_per_row");
    stages_file = open_csv(dir, "stages.csv",
                           "spec,rows_per_input,stage,bits,passes,rows_per_bucket,runs,median_ns_per_row,"
                           "min_ns_per_row,max_ns_per_row");
    FILE *best_file = open_csv(dir, "best.csv",
                               "spec,rows_per_input,role,bits,passes,rows_per_bucket,median_ns_per_row,"
                               "min_ns_per_row,max_ns_per_row,no_partitioning_ns_per_row,gain_vs_no_partitioning,"
                               "tied_settings,range_edge");

    SizeResult results[3];
    for (int i = 0; i < n_train; ++i) results[i] = tune_size(train[i], pf, best_file);

    /* The learned rule: aim for T rows of R per partition, T = geometric mean
     * over the training sizes; passes and rows per bucket = most frequent
     * winner (the largest size breaks ties). */
    double log_sum = 0;
    int partitioned = 0;
    for (int i = 0; i < n_train; ++i)
        if (results[i].win.bits) { log_sum += log2((double)train[i]) - results[i].win.bits; ++partitioned; }
    double t_log = partitioned ? log_sum / partitioned : -1;
    /* Start from the largest size's winner; another value replaces it only if it won more often. */
    unsigned rule_passes = 1;
    for (int i = n_train - 1; i >= 0; --i) if (results[i].win.passes) { rule_passes = results[i].win.passes; break; }
    double rule_load = results[n_train - 1].win.load;
    int passes_count = 0, load_count = 0;
    for (int j = 0; j < n_train; ++j) {
        passes_count += results[j].win.passes == rule_passes;
        load_count += fabs(results[j].win.load - rule_load) < 1e-9;
    }
    for (int i = 0; i < n_train; ++i) {
        int cp = 0, cl = 0;
        for (int j = 0; j < n_train; ++j) {
            cp += results[i].win.passes && results[j].win.passes == results[i].win.passes;
            cl += fabs(results[j].win.load - results[i].win.load) < 1e-9;
        }
        if (cp > passes_count) { rule_passes = results[i].win.passes; passes_count = cp; }
        if (cl > load_count) { rule_load = results[i].win.load; load_count = cl; }
    }

    char a1[96], a2[96], l1[16];
    FILE *rule = NULL;
    {
        char path[1024];
        snprintf(path, sizeof path, "%s/rule.txt", dir);
        rule = fopen(path, "w");
        if (!rule) die("cannot write rule.txt");
    }
    fprintf(rule, "spec=%d\n", SPEC_VERSION);
    printf("\n== Learned rule\n");
    if (partitioned) {
        double target = pow(2, t_log);
        fprintf(rule, "rows_per_partition=%.0f\npasses=%u\nrows_per_bucket=%g\n", target, rule_passes, rule_load);
        fprintf(rule, "formula=bits = round(log2(rows_per_input / %.0f)), %u pass(es), %s rows per bucket\n", target,
                rule_passes, load_text(rule_load, l1, sizeof l1));
        printf("  Aim for about %.0f rows of R per partition: bits = round(log2(rows / %.0f)),\n", target, target);
        printf("  %u pass%s, %s rows per bucket.\n", rule_passes, rule_passes > 1 ? "es" : "",
               load_text(rule_load, l1, sizeof l1));
    } else {
        fprintf(rule, "rows_per_partition=none\nformula=no partitioning won at any training size\n");
        printf("  No partitioning won at every training size.\n");
    }

    /* Held-out check: a size the rule was not fitted on. */
    if (test && partitioned) {
        double raw = log2((double)test) - t_log;
        unsigned bmax = max_bits_for(test);
        unsigned pb = raw < 0.5 ? 0 : (unsigned)lround(raw);
        if (pb > bmax) pb = bmax;
        Knobs pred = {pb, pb ? (rule_passes < pb ? rule_passes : pb) : 0, rule_load};
        printf("\n== Held-out check at %zu rows: the rule says %s\n", test, knob_text(pred, a1, sizeof a1));
        uint64_t t0 = now_ns();
        Work w = work_new(test);
        generate(w.r, w.s, test, SEED);
        w.expected = expected_result(test);
        measure(&w, pred);
        static Cand T[MAX_CANDS];
        int nt = 0;
        T[nt++] = (Cand){.k = {0, 0, rule_load}};
        int lo = (int)pb - pf->span_t, hi = (int)pb + pf->span_t;
        for (int b = lo < 1 ? 1 : lo; b <= hi && b <= (int)bmax; ++b)
            T[nt++] = (Cand){.k = {(unsigned)b, rule_passes < (unsigned)b ? rule_passes : (unsigned)b, rule_load}};
        double s = run_stage(&w, "T", T, nt, pf->rounds_t);
        int bi = best_index(T, nt), pi = -1, si = 0;
        for (int i = 0; i < nt; ++i) {
            if (same_knobs(T[i].k, pred)) pi = i;
            if (T[i].k.bits == 0) si = i;
        }
        if (pi < 0) pi = si; /* the rule said "no partitioning" */
        int tied = pi == bi || T[pi].min <= T[bi].max;
        double ratio = T[pi].median / T[bi].median;
        printf("  %d settings x %d rounds, %.1f s\n", nt, pf->rounds_t, s);
        printf("  best measured: %s, %.2f ns per row\n", knob_text(T[bi].k, a2, sizeof a2), T[bi].median);
        printf("  rule's pick:   %.2f ns per row, %.3fx the best%s\n", T[pi].median, ratio,
               pi == bi ? " (it is the best)" : tied ? " (tied: run ranges overlap)" : " (slower, not tied)");
        fprintf(best_file, "%d,%zu,test-best,%u,%u,%g,%.4f,%.4f,%.4f,%.4f,%.3f,,\n", SPEC_VERSION, test, T[bi].k.bits,
                T[bi].k.passes, T[bi].k.load, T[bi].median, T[bi].min, T[bi].max, T[si].median,
                T[si].median / T[bi].median);
        fprintf(best_file, "%d,%zu,test-rule,%u,%u,%g,%.4f,%.4f,%.4f,%.4f,%.3f,,\n", SPEC_VERSION, test, T[pi].k.bits,
                T[pi].k.passes, T[pi].k.load, T[pi].median, T[pi].min, T[pi].max, T[si].median,
                T[si].median / T[pi].median);
        fprintf(rule, "test_rows=%zu\ntest_rule=%s\ntest_best=%s\ntest_rule_vs_best=%.4f\ntest_tied=%s\n"
                      "test_seconds=%.1f\n",
                test, short_knobs(T[pi].k, a1, sizeof a1), short_knobs(T[bi].k, a2, sizeof a2), ratio,
                tied ? "yes" : "no", (double)(now_ns() - t0) / 1e9);
        work_free(&w);
    } else if (!test) {
        fprintf(rule, "test_rows=skipped (not enough memory)\n");
    }
    fclose(rule);

    double total = (double)(now_ns() - t_start) / 1e9;
    {
        char path[1024];
        snprintf(path, sizeof path, "%s/machine.txt", dir);
        FILE *m = fopen(path, "w");
        if (!m) die("cannot write machine.txt");
        time_t now = time(NULL);
        char date[64];
        strftime(date, sizeof date, "%Y-%m-%d %H:%M:%S", localtime(&now));
        fprintf(m, "spec=%d\nprofile=%s\ndate=%s\ncpu=%s\nmachine_tag=%s\nos=%s %s %s\nlogical_cpus=%ld\n"
                   "ram_bytes=%zu\ncompiler=%s\nthread=%s\ncache_sizes=not read (the tuner only times the join)\n"
                   "skipped_sizes=%s\n",
                SPEC_VERSION, pf->name, date, cpu, tag, u.sysname, u.release, u.machine,
                sysconf(_SC_NPROCESSORS_ONLN), ram,
#ifdef __VERSION__
                __VERSION__,
#else
                "unknown",
#endif
                prio, *dropped ? dropped : "none");
        for (int i = 0; i < n_train; ++i) fprintf(m, "seconds_%zu=%.1f\n", train[i], results[i].seconds);
        fprintf(m, "seconds_total=%.1f\n", total);
        fclose(m);
    }
    fclose(trials_file);
    fclose(stages_file);
    fclose(best_file);

    printf("\n== Tuned settings for %s (spec %d)\n", cpu, SPEC_VERSION);
    printf("  %14s  %-44s  %10s  %s\n", "rows per input", "fastest setting", "ns per row", "vs no partitioning");
    for (int i = 0; i < n_train; ++i)
        printf("  %14zu  %-44s  %10.2f  %.2fx as fast\n", train[i], knob_text(results[i].win, a1, sizeof a1),
               results[i].final_win.median, results[i].simple_median / results[i].final_win.median);
    printf("  Total time: %.0f s. Files: %s/\n", total, dir);
    return 0;
}
