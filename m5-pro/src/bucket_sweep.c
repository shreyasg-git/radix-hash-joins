/* Experiment 6: how many buckets should the hash table have, and does the
 * hash function spread our keys evenly?
 *
 * Why this exists: the professor asked how the bucket count was chosen. It
 * never was tested. Week 2 used "at least 2 buckets per row" (0.5 rows per
 * bucket); commit d28429e changed it to "about 1 bucket per 4 rows" (4 rows
 * per bucket). Neither was measured, and the hash function (mix) was never
 * checked. This program measures both, for the simple and the radix join.
 *
 * Dimensions. Every combination of the lists is one setting:
 *   --sizes     rows per input          default 65536,524288,4194304,16777216
 *   --loads     rows per bucket         default 0.0625,0.125,0.25,0.5,1,2,4,8
 *               The table gets the smallest power-of-two number of buckets
 *               that keeps at most this many rows per bucket on average.
 *               0.5 is the Week 2 rule, 4 the rule of commit d28429e.
 *   --hashes    identity,multiply-low,fibonacci,crc32,mix     default mix
 *   --keys      dense,stride64,random                         default dense
 *   --joins     simple,radix                                  default both
 *   --bits      radix bits, or auto = log2(rows / 4096)       default auto
 *   --repeats R (default 7)   --seed S (default 42)
 *
 * Hash functions (the bucket is the low bits of the hash, as in experiments 2
 * to 5; the radix join takes its partition from the low bits and the bucket
 * from the bits above them):
 *   identity      the key itself
 *   multiply-low  key * 0x9E3779B97F4A7C15, low bits (low bits of a product
 *                 only depend on the low bits of the key)
 *   fibonacci     the same product read from its top bit down (Knuth's
 *                 multiplicative hashing: the top bits depend on every key
 *                 bit). The bits are reversed so the top bits come first.
 *   crc32         the CRC-32C instruction of the processor
 *   mix           the SplitMix64 finalizer used by every other experiment
 * Key patterns (left keys k(0..N-1), right keys k(0), k(2), .. k(2N-2), so
 * half of the right rows match, exactly as in experiments 2 to 5):
 *   dense     k(j) = j                      (the pattern of every experiment)
 *   stride64  k(j) = 64 j                   (the low 6 bits are always 0)
 *   random    k(j) = murmur3 finalizer of j (unique, random-looking)
 *
 * Timing follows experiment 4 after its fix: memory is allocated and written
 * before any timer starts; clearing the bucket array is build time; the radix
 * join's build-and-probe loop is timed once with no clock reads inside it, and
 * its build time comes from a separate build-only pass after each run.
 * Each round runs every setting of one input size once in a newly shuffled
 * order. Run 0 is a verified warmup.
 *
 * Output (a NEW folder; earlier results are never overwritten):
 *   timings.csv       one row per recorded run
 *   distribution.csv  one row per setting: how evenly the keys spread over the
 *                     buckets, measured on the real tables (not timed)
 *   environment.txt   machine, compiler, settings
 *
 * Build:  make
 * Test:   ./bin/bucket_sweep --test
 * Run:    ./bin/bucket_sweep results/06-bucket-sweep [options]
 */
#include "common.h"
#include <math.h>
#if defined(__aarch64__) && defined(__ARM_FEATURE_CRC32)
#include <arm_acle.h>
#define CRC_SOURCE "ARM CRC32CW instruction"
static inline uint32_t crc32c_u32(uint32_t value) { return __crc32cw(0xFFFFFFFFu, value); }
#elif defined(__SSE4_2__)
#include <nmmintrin.h>
#define CRC_SOURCE "x86 CRC32 instruction"
static inline uint32_t crc32c_u32(uint32_t value) { return _mm_crc32_u32(0xFFFFFFFFu, value); }
#else
#define CRC_SOURCE "software (no CRC instruction available)"
static inline uint32_t crc32c_u32(uint32_t value) {
    uint32_t crc = 0xFFFFFFFFu ^ value;
    for (int bit = 0; bit < 32; ++bit) crc = (crc >> 1) ^ (0x82F63B78u & (0u - (crc & 1)));
    return crc;
}
#endif

#define MAX_LIST 16
#define AUTO_PARTITION_ROWS 4096

enum { H_IDENTITY, H_MULTIPLY_LOW, H_FIBONACCI, H_CRC32, H_MIX, HASH_COUNT };
static const char *hash_name[HASH_COUNT] = {"identity", "multiply-low", "fibonacci", "crc32", "mix"};
enum { K_DENSE, K_STRIDE64, K_RANDOM, KEY_COUNT };
static const char *key_name[KEY_COUNT] = {"dense", "stride64", "random"};
enum { J_SIMPLE, J_RADIX, JOIN_COUNT };
static const char *join_name[JOIN_COUNT] = {"simple", "radix"};

/* ---------- hash functions ----------------------------------------------- */

#define ALWAYS_INLINE static inline __attribute__((always_inline))

ALWAYS_INLINE uint64_t reverse_bits(uint64_t x) {
#if defined(__clang__)
    return __builtin_bitreverse64(x); /* one instruction (RBIT) on ARM */
#else
    x = ((x >> 1) & UINT64_C(0x5555555555555555)) | ((x & UINT64_C(0x5555555555555555)) << 1);
    x = ((x >> 2) & UINT64_C(0x3333333333333333)) | ((x & UINT64_C(0x3333333333333333)) << 2);
    x = ((x >> 4) & UINT64_C(0x0F0F0F0F0F0F0F0F)) | ((x & UINT64_C(0x0F0F0F0F0F0F0F0F)) << 4);
    return __builtin_bswap64(x);
#endif
}

ALWAYS_INLINE uint64_t hash_of(int hash, uint32_t key) {
    switch (hash) {
    case H_IDENTITY: return key;
    case H_MULTIPLY_LOW: return (uint64_t)key * UINT64_C(0x9E3779B97F4A7C15);
    case H_FIBONACCI: return reverse_bits((uint64_t)key * UINT64_C(0x9E3779B97F4A7C15));
    case H_CRC32: return crc32c_u32(key);
    default: return mix(key);
    }
}

/* Runs the code with H set to each hash as a compile-time constant, so every
 * hash gets its own fully inlined loop (no switch inside the timed loops). */
#define FOR_HASH(hash, ...)                                                   \
    switch (hash) {                                                           \
    case H_IDENTITY: { enum { H = H_IDENTITY }; __VA_ARGS__; } break;         \
    case H_MULTIPLY_LOW: { enum { H = H_MULTIPLY_LOW }; __VA_ARGS__; } break; \
    case H_FIBONACCI: { enum { H = H_FIBONACCI }; __VA_ARGS__; } break;       \
    case H_CRC32: { enum { H = H_CRC32 }; __VA_ARGS__; } break;               \
    default: { enum { H = H_MIX }; __VA_ARGS__; } break;                      \
    }

/* ---------- key patterns --------------------------------------------------- */

static inline uint32_t murmur_finalizer(uint32_t h) { /* a bijection on 32-bit values */
    h ^= h >> 16;
    h *= 0x85ebca6bu;
    h ^= h >> 13;
    h *= 0xc2b2ae35u;
    return h ^ (h >> 16);
}

static inline uint32_t key_of(int pattern, size_t j) {
    switch (pattern) {
    case K_STRIDE64: return (uint32_t)(j * 64);
    case K_RANDOM: return murmur_finalizer((uint32_t)j);
    default: return (uint32_t)j;
    }
}

/* Left row i has key k(i), right row i has key k(2i); both tables shuffled.
 * k is one-to-one, so exactly the right rows with 2i < N match, and the answer
 * equals expected_result(N) for every pattern. */
static void generate_pattern(Record *left, Record *right, size_t count, int pattern, uint64_t seed) {
    for (size_t i = 0; i < count; ++i) {
        left[i] = (Record){key_of(pattern, i), (uint32_t)i};
        right[i] = (Record){key_of(pattern, 2 * i), (uint32_t)i};
    }
    shuffle(left, count, &seed);
    shuffle(right, count, &seed);
}

/* ---------- hash table with a chosen number of rows per bucket ------------- */

static size_t buckets_for(size_t rows, double load) {
    double need = ceil((double)rows / load);
    size_t count = 1;
    while ((double)count < need) count *= 2;
    return count;
}

typedef struct {
    const Record *records;
    uint32_t *buckets, *next;
    size_t mask;
    unsigned shift;
} Table;

ALWAYS_INLINE void build_h(int hash, Table *t, const Record *records, size_t count, size_t bucket_count,
                           uint32_t *buckets, uint32_t *next, unsigned shift) {
    t->records = records;
    t->buckets = buckets;
    t->next = next;
    t->mask = bucket_count - 1;
    t->shift = shift;
    for (size_t i = 0; i < bucket_count; ++i) buckets[i] = EMPTY;
    for (size_t i = 0; i < count; ++i) {
        size_t bucket = (hash_of(hash, records[i].key) >> shift) & t->mask;
        next[i] = buckets[bucket];
        buckets[bucket] = (uint32_t)i;
    }
}

ALWAYS_INLINE void probe_h(int hash, const Table *t, const Record *right, size_t count, JoinResult *result) {
    for (size_t i = 0; i < count; ++i) {
        uint32_t key = right[i].key;
        size_t bucket = (hash_of(hash, key) >> t->shift) & t->mask;
        for (uint32_t row = t->buckets[bucket]; row != EMPTY; row = t->next[row]) {
            if (t->records[row].key == key) {
                ++result->matches;
                result->checksum += pair_hash(t->records[row].row_id, right[i].row_id);
            }
        }
    }
}

/* One radix pass on the low `bits` bits of the hash: count, prefix sum, copy.
 * bounds gets 2^bits + 1 entries. */
ALWAYS_INLINE void partition_h(int hash, const Record *src, Record *dst, size_t count, unsigned bits, size_t *bounds) {
    const size_t fanout = (size_t)1 << bits, mask = fanout - 1;
    for (size_t i = 0; i <= fanout; ++i) bounds[i] = 0;
    for (size_t i = 0; i < count; ++i) ++bounds[(hash_of(hash, src[i].key) & mask) + 1];
    for (size_t i = 1; i <= fanout; ++i) bounds[i] += bounds[i - 1];
    for (size_t i = 0; i < count; ++i) dst[bounds[hash_of(hash, src[i].key) & mask]++] = src[i];
    for (size_t i = fanout; i > 0; --i) bounds[i] = bounds[i - 1];
    bounds[0] = 0;
}

/* ---------- the two joins -------------------------------------------------- */

typedef struct {
    Record *r, *s;              /* partitioned copies of R and S */
    size_t *r_bounds, *s_bounds;
    uint32_t *buckets, *next;   /* bucket array big enough for every setting */
} Buffers;

typedef struct {
    double partition_ms, build_ms, probe_ms, join_ms, total_ms;
    size_t buckets; /* simple: bucket count; radix: sum over all partition tables */
    JoinResult result;
} Times;

static Times simple_join(int hash, const Record *left, size_t ln, const Record *right, size_t rn, double load,
                         Buffers *b) {
    Times t = {0};
    size_t bucket_count = buckets_for(ln, load);
    FOR_HASH(hash, {
        Table table;
        uint64_t start = now_ns();
        build_h(H, &table, left, ln, bucket_count, b->buckets, b->next, 0);
        uint64_t built = now_ns();
        probe_h(H, &table, right, rn, &t.result);
        uint64_t done = now_ns();
        t.build_ms = (built - start) / 1e6;
        t.probe_ms = (done - built) / 1e6;
        t.join_ms = t.total_ms = (done - start) / 1e6;
    });
    t.buckets = bucket_count;
    return t;
}

static Times radix_join(int hash, const Record *left, size_t ln, const Record *right, size_t rn, double load,
                        unsigned bits, Buffers *b) {
    Times t = {0};
    const size_t partitions = (size_t)1 << bits;
    FOR_HASH(hash, {
        uint64_t start = now_ns();
        partition_h(H, left, b->r, ln, bits, b->r_bounds);
        partition_h(H, right, b->s, rn, bits, b->s_bounds);
        uint64_t joined_from = now_ns();
        for (size_t p = 0; p < partitions; ++p) {
            size_t r0 = b->r_bounds[p], r1 = b->r_bounds[p + 1];
            Table table;
            build_h(H, &table, b->r + r0, r1 - r0, buckets_for(r1 - r0, load), b->buckets, b->next, bits);
            probe_h(H, &table, b->s + b->s_bounds[p], b->s_bounds[p + 1] - b->s_bounds[p], &t.result);
        }
        uint64_t done = now_ns();
        /* Build-only pass for the phase split; not part of the total. */
        uint64_t build_from = now_ns();
        for (size_t p = 0; p < partitions; ++p) {
            size_t r0 = b->r_bounds[p], r1 = b->r_bounds[p + 1];
            Table table;
            build_h(H, &table, b->r + r0, r1 - r0, buckets_for(r1 - r0, load), b->buckets, b->next, bits);
        }
        uint64_t build_ns = now_ns() - build_from;
        t.partition_ms = (joined_from - start) / 1e6;
        t.join_ms = (done - joined_from) / 1e6;
        t.build_ms = build_ns / 1e6;
        t.probe_ms = t.join_ms > t.build_ms ? t.join_ms - t.build_ms : 0;
        t.total_ms = (done - start) / 1e6;
    });
    for (size_t p = 0; p < partitions; ++p) t.buckets += buckets_for(b->r_bounds[p + 1] - b->r_bounds[p], load);
    return t;
}

/* ---------- how evenly the keys spread (measured, not timed) --------------- */

typedef struct {
    size_t buckets, used_buckets, longest_chain, partitions_used, largest_partition;
    double rows_checked_per_probe;
} Spread;

/* Chain length of every bucket = number of left rows in it. A probe walks the
 * whole chain of its bucket, so rows checked per probe = mean chain length of
 * the buckets the right rows fall into. Uses b->buckets as a counter array. */
static void spread_of_table(int hash, const Record *left, size_t ln, const Record *right, size_t rn,
                            size_t bucket_count, unsigned shift, uint32_t *count, Spread *sp, double *checked) {
    FOR_HASH(hash, {
        const size_t mask = bucket_count - 1;
        for (size_t i = 0; i < bucket_count; ++i) count[i] = 0;
        for (size_t i = 0; i < ln; ++i) ++count[(hash_of(H, left[i].key) >> shift) & mask];
        for (size_t i = 0; i < bucket_count; ++i) {
            if (count[i]) ++sp->used_buckets;
            if (count[i] > sp->longest_chain) sp->longest_chain = count[i];
        }
        for (size_t i = 0; i < rn; ++i) *checked += count[(hash_of(H, right[i].key) >> shift) & mask];
    });
    sp->buckets += bucket_count;
}

static Spread measure_spread(int join, int hash, const Record *left, size_t ln, const Record *right, size_t rn,
                             double load, unsigned bits, Buffers *b) {
    Spread sp = {0};
    double checked = 0;
    if (join == J_SIMPLE) {
        spread_of_table(hash, left, ln, right, rn, buckets_for(ln, load), 0, b->buckets, &sp, &checked);
        sp.partitions_used = 1;
        sp.largest_partition = ln;
    } else {
        FOR_HASH(hash, {
            partition_h(H, left, b->r, ln, bits, b->r_bounds);
            partition_h(H, right, b->s, rn, bits, b->s_bounds);
        });
        for (size_t p = 0; p < ((size_t)1 << bits); ++p) {
            size_t r0 = b->r_bounds[p], rows = b->r_bounds[p + 1] - r0;
            if (rows) ++sp.partitions_used;
            if (rows > sp.largest_partition) sp.largest_partition = rows;
            spread_of_table(hash, b->r + r0, rows, b->s + b->s_bounds[p], b->s_bounds[p + 1] - b->s_bounds[p],
                            buckets_for(rows, load), bits, b->buckets, &sp, &checked);
        }
    }
    sp.rows_checked_per_probe = rn ? checked / (double)rn : 0;
    return sp;
}

/* ---------- buffers -------------------------------------------------------- */

static Buffers make_buffers(size_t rows, double smallest_load, unsigned max_bits, int touched) {
    Buffers b;
    size_t n = rows ? rows : 1, buckets = buckets_for(n, smallest_load), bounds = ((size_t)1 << max_bits) + 1;
    if (touched) {
        b.r = allocate_touched(n * sizeof(Record), PAGES_DEFAULT);
        b.s = allocate_touched(n * sizeof(Record), PAGES_DEFAULT);
        b.r_bounds = allocate_touched(bounds * sizeof(size_t), PAGES_DEFAULT);
        b.s_bounds = allocate_touched(bounds * sizeof(size_t), PAGES_DEFAULT);
        b.buckets = allocate_touched(buckets * sizeof(uint32_t), PAGES_DEFAULT);
        b.next = allocate_touched(n * sizeof(uint32_t), PAGES_DEFAULT);
    } else {
        b.r = allocate(n, sizeof(Record));
        b.s = allocate(n, sizeof(Record));
        b.r_bounds = allocate(bounds, sizeof(size_t));
        b.s_bounds = allocate(bounds, sizeof(size_t));
        b.buckets = allocate(buckets, sizeof(uint32_t));
        b.next = allocate(n, sizeof(uint32_t));
    }
    return b;
}

static void free_buffers(Buffers *b) {
    free(b->r); free(b->s); free(b->r_bounds); free(b->s_bounds); free(b->buckets); free(b->next);
}

static unsigned auto_bits(size_t rows) {
    double bits = round(log2((double)rows / AUTO_PARTITION_ROWS));
    if (bits < 1) bits = 1;
    if (bits > 16) bits = 16;
    return (unsigned)bits;
}

/* ---------- correctness ---------------------------------------------------- */

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

static const double test_loads[] = {0.25, 0.5, 1, 3, 4, 16};

static void check_all(const char *name, const Record *left, size_t ln, const Record *right, size_t rn,
                      JoinResult want, size_t *cases) {
    size_t most = ln > rn ? ln : rn;
    Buffers b = make_buffers(most, 0.25, 8, 0);
    for (int hash = 0; hash < HASH_COUNT; ++hash) {
        for (size_t li = 0; li < sizeof(test_loads) / sizeof(test_loads[0]); ++li) {
            double load = test_loads[li];
            Times t = simple_join(hash, left, ln, right, rn, load, &b);
            if (t.result.matches != want.matches || t.result.checksum != want.checksum) {
                fprintf(stderr, "FAIL simple hash=%s load=%g: %s\n", hash_name[hash], load, name);
                exit(EXIT_FAILURE);
            }
            ++*cases;
            for (unsigned bits = 1; bits <= 8; bits += 3) {
                t = radix_join(hash, left, ln, right, rn, load, bits, &b);
                if (t.result.matches != want.matches || t.result.checksum != want.checksum) {
                    fprintf(stderr, "FAIL radix hash=%s load=%g bits=%u: %s\n", hash_name[hash], load, bits, name);
                    exit(EXIT_FAILURE);
                }
                ++*cases;
            }
        }
    }
    free_buffers(&b);
}

static void self_test(void) {
    size_t cases = 0;
    /* The bucket rule must reproduce both rules from the history. */
    for (size_t rows = 1; rows < 5000; rows += 7) {
        size_t quarter = 1, minimum = rows / 4 + (rows % 4 != 0);
        while (quarter < minimum) quarter *= 2;
        if (buckets_for(rows, 0.5) != bucket_count_for(rows)) die("FAIL: load 0.5 is not the Week 2 rule");
        if (buckets_for(rows, 4) != quarter) die("FAIL: load 4 is not the rule of commit d28429e");
        cases += 2;
    }
    /* Key patterns are one-to-one, so the known answer holds for every pattern. */
    for (int pattern = 0; pattern < KEY_COUNT; ++pattern) {
        size_t n = 3000;
        Record *l = allocate(n, sizeof(Record)), *r = allocate(n, sizeof(Record));
        generate_pattern(l, r, n, pattern, 42);
        JoinResult want = expected_result(n), got = nested_loop(l, n, r, n);
        if (want.matches != got.matches || want.checksum != got.checksum) die("FAIL: generated pattern answer");
        check_all(key_name[pattern], l, n, r, n, want, &cases);
        free(l); free(r);
    }
    /* Duplicates, boundary keys, empty inputs. */
    Record left[] = {{7, 0}, {7, 1}, {9, 2}, {UINT32_MAX, 3}, {0, 4}};
    Record right[] = {{7, 0}, {7, 1}, {0, 2}, {UINT32_MAX, 3}, {42, 4}};
    check_all("duplicates and boundary keys", left, 5, right, 5, nested_loop(left, 5, right, 5), &cases);
    check_all("empty left", NULL, 0, right, 5, (JoinResult){0, 0}, &cases);
    check_all("empty right", left, 5, NULL, 0, (JoinResult){0, 0}, &cases);
    uint64_t state = 99;
    for (size_t trial = 0; trial < 30; ++trial) {
        size_t ln = 1 + random_next(&state) % 300, rn = 1 + random_next(&state) % 300;
        uint32_t range = 1 + (uint32_t)(random_next(&state) % 200);
        Record *a = allocate(ln, sizeof(Record)), *c = allocate(rn, sizeof(Record));
        for (size_t i = 0; i < ln; ++i) a[i] = (Record){(uint32_t)(random_next(&state) % range) * 64, (uint32_t)i};
        for (size_t i = 0; i < rn; ++i) c[i] = (Record){(uint32_t)(random_next(&state) % range) * 64, (uint32_t)i};
        check_all("random duplicate keys", a, ln, c, rn, nested_loop(a, ln, c, rn), &cases);
        free(a); free(c);
    }
    /* The spread measurement: a perfect spread for dense keys with identity. */
    {
        size_t n = 1024;
        Record *l = allocate(n, sizeof(Record)), *r = allocate(n, sizeof(Record));
        generate_pattern(l, r, n, K_DENSE, 1);
        Buffers b = make_buffers(n, 0.5, 8, 0);
        Spread sp = measure_spread(J_SIMPLE, H_IDENTITY, l, n, r, n, 1, 0, &b);
        if (sp.buckets != 1024 || sp.used_buckets != 1024 || sp.longest_chain != 1) die("FAIL: spread of identity");
        sp = measure_spread(J_SIMPLE, H_IDENTITY, l, n, r, n, 0.5, 0, &b);
        /* 2048 buckets, right keys 0,2,..: probes with key < 2048 land on 1 row, the rest on buckets 0..2047 again */
        if (sp.used_buckets != 1024 || sp.longest_chain != 1) die("FAIL: spread of identity at load 0.5");
        ++cases;
        free_buffers(&b); free(l); free(r);
    }
    printf("Passed %zu checks: both bucket rules, %d key patterns, %d hash functions, simple and radix join "
           "against the nested-loop reference.\n", cases, KEY_COUNT, HASH_COUNT);
}

/* ---------- command line --------------------------------------------------- */

static size_t parse_names(const char *text, const char **names, int name_count, int *out) {
    char buffer[512];
    if (strlen(text) >= sizeof(buffer)) die("List argument is too long");
    strcpy(buffer, text);
    size_t count = 0;
    for (char *item = strtok(buffer, ","); item; item = strtok(NULL, ",")) {
        int found = -1;
        for (int i = 0; i < name_count; ++i) if (!strcmp(item, names[i])) found = i;
        if (found < 0) {
            fprintf(stderr, "Unknown name '%s'. Choose from:", item);
            for (int i = 0; i < name_count; ++i) fprintf(stderr, " %s", names[i]);
            fputc('\n', stderr);
            exit(EXIT_FAILURE);
        }
        if (count == MAX_LIST) die("Too many list items");
        out[count++] = found;
    }
    if (!count) die("Empty list argument");
    return count;
}

static size_t parse_loads(const char *text, double *out) {
    char buffer[512];
    if (strlen(text) >= sizeof(buffer)) die("List argument is too long");
    strcpy(buffer, text);
    size_t count = 0;
    for (char *item = strtok(buffer, ","); item; item = strtok(NULL, ",")) {
        char *end;
        double value = strtod(item, &end);
        if (*end || !(value >= 1.0 / 64) || value > 1024) die("Rows per bucket must be between 1/64 and 1024");
        if (count == MAX_LIST) die("Too many list items");
        out[count++] = value;
    }
    if (!count) die("Empty list argument");
    return count;
}

typedef struct { int join, hash, keys; double load; } Setting;

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--test")) { self_test(); return 0; }
    if (argc < 2 || argv[1][0] == '-') {
        fprintf(stderr, "Usage: %s --test\n       %s OUTPUT_DIRECTORY [--sizes N,N] [--loads 0.5,4] "
                "[--hashes mix,crc32] [--keys dense,random] [--joins simple,radix] [--bits auto|B] "
                "[--repeats R] [--seed S]\n", argv[0], argv[0]);
        return EXIT_FAILURE;
    }
    size_t sizes[MAX_LIST] = {65536, 524288, 4194304, 16777216}, size_count = 4;
    double loads[MAX_LIST] = {0.0625, 0.125, 0.25, 0.5, 1, 2, 4, 8};
    size_t load_count = 8;
    int hashes[MAX_LIST] = {H_MIX}, keys[MAX_LIST] = {K_DENSE}, joins[MAX_LIST] = {J_SIMPLE, J_RADIX};
    size_t hash_count = 1, keys_count = 1, join_count = 2, repeats = 7, seed = 42, fixed_bits = 0;
    for (int i = 2; i < argc; i += 2) {
        if (i + 1 >= argc) die("Missing value after option");
        if (!strcmp(argv[i], "--sizes")) size_count = parse_list(argv[i + 1], sizes, MAX_LIST, 1000, (size_t)1 << 25);
        else if (!strcmp(argv[i], "--loads")) load_count = parse_loads(argv[i + 1], loads);
        else if (!strcmp(argv[i], "--hashes")) hash_count = parse_names(argv[i + 1], hash_name, HASH_COUNT, hashes);
        else if (!strcmp(argv[i], "--keys")) keys_count = parse_names(argv[i + 1], key_name, KEY_COUNT, keys);
        else if (!strcmp(argv[i], "--joins")) join_count = parse_names(argv[i + 1], join_name, JOIN_COUNT, joins);
        else if (!strcmp(argv[i], "--bits")) fixed_bits = strcmp(argv[i + 1], "auto") ? parse_number(argv[i + 1], 1, 16) : 0;
        else if (!strcmp(argv[i], "--repeats")) repeats = parse_number(argv[i + 1], 1, 100);
        else if (!strcmp(argv[i], "--seed")) seed = parse_number(argv[i + 1], 0, UINT32_MAX);
        else die("Unknown option");
    }
    double smallest_load = loads[0];
    for (size_t i = 1; i < load_count; ++i) if (loads[i] < smallest_load) smallest_load = loads[i];

    make_results_directory(argv[1]);
    int cpu = pin_to_cpu(0);
    FILE *env = open_in(argv[1], "environment.txt");
    write_common_environment(env);
    fprintf(env, "experiment=bucket_sweep\nlogical_cpu=%d\nrepeats=%zu\nwarmup_runs=1\nseed=%zu\n"
            "run_order=shuffled every round\nradix_bits=%s\nradix_passes=1\ncrc32=%s\n"
            "timing=clean (no clock reads inside the join; radix build split from a separate build-only pass)\n"
            "bucket_rule=smallest power of two with at most rows_per_bucket rows per bucket on average\n",
            cpu, repeats, seed, fixed_bits ? "fixed" : "auto (log2(rows / 4096), about 4096 rows per partition)",
            CRC_SOURCE);
    fprintf(env, "timer_call_ns=%.1f\n", timer_cost_ns());
    fclose(env);

    FILE *out = open_in(argv[1], "timings.csv");
    fputs("rows_per_input,join,hash,keys,rows_per_bucket,bits,buckets,run,position,partition_ms,build_ms,probe_ms,"
          "join_ms,total_ms,matches\n", out);
    FILE *spread_file = open_in(argv[1], "distribution.csv");
    fputs("rows_per_input,join,hash,keys,rows_per_bucket,bits,buckets,actual_rows_per_bucket,empty_bucket_share,"
          "longest_chain,rows_checked_per_probe,partitions,partitions_used,largest_partition_rows\n", spread_file);

    for (size_t si = 0; si < size_count; ++si) {
        size_t rows = sizes[si];
        unsigned bits = fixed_bits ? (unsigned)fixed_bits : auto_bits(rows);
        Record *left[KEY_COUNT] = {0}, *right[KEY_COUNT] = {0};
        for (size_t k = 0; k < keys_count; ++k) {
            int pattern = keys[k];
            if (left[pattern]) continue;
            if (pattern == K_STRIDE64 && 2 * rows * 64 > ((size_t)1 << 32)) die("stride64 keys need at most 2^25 rows");
            left[pattern] = allocate_touched(rows * sizeof(Record), PAGES_DEFAULT);
            right[pattern] = allocate_touched(rows * sizeof(Record), PAGES_DEFAULT);
            generate_pattern(left[pattern], right[pattern], rows, pattern, seed);
        }
        JoinResult expected = expected_result(rows);
        Buffers b = make_buffers(rows, smallest_load, bits, 1);

        size_t setting_count = 0;
        Setting *settings = allocate(join_count * hash_count * keys_count * load_count, sizeof(Setting));
        for (size_t j = 0; j < join_count; ++j)
            for (size_t h = 0; h < hash_count; ++h)
                for (size_t k = 0; k < keys_count; ++k)
                    for (size_t l = 0; l < load_count; ++l) {
                        if (joins[j] == J_RADIX && ((size_t)1 << bits) > rows / 4) continue;
                        settings[setting_count++] = (Setting){joins[j], hashes[h], keys[k], loads[l]};
                    }
        /* The spread of every setting, measured once before any timing. */
        for (size_t c = 0; c < setting_count; ++c) {
            Setting st = settings[c];
            unsigned sb = st.join == J_RADIX ? bits : 0;
            Spread sp = measure_spread(st.join, st.hash, left[st.keys], rows, right[st.keys], rows, st.load, sb, &b);
            fprintf(spread_file, "%zu,%s,%s,%s,%g,%u,%zu,%.4f,%.6f,%zu,%.4f,%zu,%zu,%zu\n", rows, join_name[st.join],
                    hash_name[st.hash], key_name[st.keys], st.load, sb, sp.buckets, (double)rows / (double)sp.buckets,
                    1.0 - (double)sp.used_buckets / (double)sp.buckets, sp.longest_chain, sp.rows_checked_per_probe,
                    (size_t)1 << sb, sp.partitions_used, sp.largest_partition);
        }
        fflush(spread_file);

        size_t *order = allocate(setting_count, sizeof(size_t));
        uint64_t order_state = 6000 + rows;
        for (size_t run = 0; run <= repeats; ++run) { /* run 0 is a verified warmup */
            shuffle_order(order, setting_count, &order_state);
            for (size_t position = 0; position < setting_count; ++position) {
                Setting st = settings[order[position]];
                const Record *l = left[st.keys], *r = right[st.keys];
                Times t = st.join == J_SIMPLE ? simple_join(st.hash, l, rows, r, rows, st.load, &b)
                                              : radix_join(st.hash, l, rows, r, rows, st.load, bits, &b);
                if (t.result.matches != expected.matches || t.result.checksum != expected.checksum)
                    die("Join result does not match the expected answer");
                if (!run) continue;
                fprintf(out, "%zu,%s,%s,%s,%g,%u,%zu,%zu,%zu,%.6f,%.6f,%.6f,%.6f,%.6f,%" PRIu64 "\n", rows,
                        join_name[st.join], hash_name[st.hash], key_name[st.keys], st.load,
                        st.join == J_RADIX ? bits : 0, t.buckets, run, position, t.partition_ms, t.build_ms,
                        t.probe_ms, t.join_ms, t.total_ms, t.result.matches);
            }
            fflush(out);
        }
        printf("%zu rows per input: %zu settings, every run verified (%" PRIu64 " matches)\n", rows, setting_count,
               expected.matches);
        fflush(stdout);
        free(order); free(settings);
        for (int k = 0; k < KEY_COUNT; ++k) { free(left[k]); free(right[k]); }
        free_buffers(&b);
    }
    fclose(out);
    fclose(spread_file);
    return 0;
}
