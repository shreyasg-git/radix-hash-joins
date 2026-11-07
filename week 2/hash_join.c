#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>

/* Each record is a join key and the row number in its original table. */
typedef struct {
    uint32_t key;
    uint32_t row_id;
} Record;

typedef struct {
    uint32_t left_row;
    uint32_t right_row;
} Pair;

typedef struct {
    const Record *records;
    size_t *buckets;
    size_t *next;
    size_t bucket_count;
} HashTable;

typedef struct {
    uint64_t matches;
    uint64_t checksum;
} JoinResult;

static void *allocate(size_t count, size_t size) {
    if (size && count > SIZE_MAX / size) {
        fprintf(stderr, "Allocation size overflow\n");
        exit(EXIT_FAILURE);
    }
    void *memory = malloc(count ? count * size : 1);
    if (!memory) {
        perror("malloc");
        exit(EXIT_FAILURE);
    }
    return memory;
}

static uint64_t mix(uint64_t value) {
    value ^= value >> 30;
    value *= UINT64_C(0xbf58476d1ce4e5b9);
    value ^= value >> 27;
    value *= UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

static uint64_t now_ns(void) {
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time)) {
        perror("clock_gettime");
        exit(EXIT_FAILURE);
    }
    return (uint64_t)time.tv_sec * UINT64_C(1000000000) + time.tv_nsec;
}

static HashTable build(const Record *records, size_t count) {
    size_t bucket_count = 1;
    while (bucket_count < count * 2) bucket_count *= 2;
    HashTable table = {records, allocate(bucket_count, sizeof(size_t)),
                       allocate(count, sizeof(size_t)), bucket_count};
    for (size_t i = 0; i < bucket_count; ++i) table.buckets[i] = SIZE_MAX;
    for (size_t i = 0; i < count; ++i) {
        size_t bucket = mix(records[i].key) & (bucket_count - 1);
        table.next[i] = table.buckets[bucket];
        table.buckets[bucket] = i;
    }
    return table;
}

/* In benchmarks we count matches. Small correctness tests also save row pairs. */
static JoinResult probe(const HashTable *table, const Record *right, size_t count,
                        Pair *pairs, size_t capacity) {
    JoinResult result = {0, 0};
    for (size_t i = 0; i < count; ++i) {
        size_t bucket = mix(right[i].key) & (table->bucket_count - 1);
        for (size_t row = table->buckets[bucket]; row != SIZE_MAX; row = table->next[row]) {
            if (table->records[row].key == right[i].key) {
                Pair pair = {table->records[row].row_id, right[i].row_id};
                if (pairs) {
                    if (result.matches >= capacity) {
                        fprintf(stderr, "Pair output buffer is too small\n");
                        exit(EXIT_FAILURE);
                    }
                    pairs[result.matches] = pair;
                }
                ++result.matches;
                result.checksum += mix(((uint64_t)pair.left_row << 32) | pair.right_row);
            }
        }
    }
    return result;
}

static void release(HashTable *table) {
    free(table->buckets);
    free(table->next);
}

static size_t reference_join(const Record *left, size_t left_count,
                             const Record *right, size_t right_count, Pair *pairs) {
    size_t count = 0;
    for (size_t i = 0; i < left_count; ++i) {
        for (size_t j = 0; j < right_count; ++j) {
            if (left[i].key == right[j].key) {
                pairs[count++] = (Pair){left[i].row_id, right[j].row_id};
            }
        }
    }
    return count;
}

static int compare_pairs(const void *first, const void *second) {
    const Pair *a = first, *b = second;
    if (a->left_row != b->left_row) return a->left_row < b->left_row ? -1 : 1;
    return (a->right_row > b->right_row) - (a->right_row < b->right_row);
}

static void check_case(const char *name, const Record *left, size_t left_count,
                       const Record *right, size_t right_count) {
    size_t capacity = left_count * right_count;
    Pair *expected = allocate(capacity, sizeof(Pair));
    Pair *actual = allocate(capacity, sizeof(Pair));
    size_t expected_count = reference_join(left, left_count, right, right_count, expected);
    HashTable table = build(left, left_count);
    JoinResult result = probe(&table, right, right_count, actual, capacity);
    qsort(expected, expected_count, sizeof(Pair), compare_pairs);
    qsort(actual, (size_t)result.matches, sizeof(Pair), compare_pairs);
    if (result.matches != expected_count || memcmp(actual, expected, expected_count * sizeof(Pair))) {
        fprintf(stderr, "FAIL: %s\n", name);
        exit(EXIT_FAILURE);
    }
    release(&table);
    free(expected);
    free(actual);
}

static uint64_t random_next(uint64_t *state) {
    *state += UINT64_C(0x9e3779b97f4a7c15);
    return mix(*state);
}

static void self_test(void) {
    Record left[] = {{7,0},{7,1},{9,2},{UINT32_MAX,3},{0,4}};
    Record right[] = {{7,0},{7,1},{0,2},{UINT32_MAX,3},{42,4}};
    Record other[] = {{22,0},{23,1}};
    check_case("duplicates and boundary keys", left, 5, right, 5);
    check_case("no matches", left, 5, other, 2);
    check_case("empty left", NULL, 0, right, 5);
    check_case("empty right", left, 5, NULL, 0);
    check_case("both empty", NULL, 0, NULL, 0);
    check_case("all combinations match", left, 2, right, 2);
    Record collisions[3];
    size_t found = 0;
    for (uint32_t key = 0; found < 3; ++key) {
        if ((mix(key) & 7) == 0) {
            collisions[found] = (Record){key, (uint32_t)found};
            ++found;
        }
    }
    check_case("different keys in the same bucket", collisions, 3, collisions, 3);
    uint64_t state = 17;
    for (size_t trial = 0; trial < 100; ++trial) {
        Record a[32], b[48];
        for (size_t i = 0; i < 32; ++i) a[i] = (Record){random_next(&state) % 11, (uint32_t)i};
        for (size_t i = 0; i < 48; ++i) b[i] = (Record){random_next(&state) % 11, (uint32_t)i};
        check_case("random duplicate keys", a, 32, b, 48);
    }
    puts("Passed 107 cases against the nested-loop reference, comparing every matching row pair.");
}

static void shuffle(Record *records, size_t count, uint64_t *state) {
    for (size_t i = count; i > 1; --i) {
        size_t j = random_next(state) % i;
        Record temporary = records[i - 1];
        records[i - 1] = records[j];
        records[j] = temporary;
    }
}

static void generate(Record *left, Record *right, size_t count, uint64_t seed) {
    for (size_t i = 0; i < count; ++i) {
        left[i] = (Record){(uint32_t)i, (uint32_t)i};
        right[i] = (Record){(uint32_t)(2 * i), (uint32_t)i};
    }
    shuffle(left, count, &seed);
    shuffle(right, count, &seed);
}

static int pin_cpu(void) {
    cpu_set_t available, selected;
    if (sched_getaffinity(0, sizeof(available), &available)) {
        perror("sched_getaffinity");
        exit(EXIT_FAILURE);
    }
    for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) {
        if (CPU_ISSET(cpu, &available)) {
            CPU_ZERO(&selected);
            CPU_SET(cpu, &selected);
            if (sched_setaffinity(0, sizeof(selected), &selected)) {
                perror("sched_setaffinity");
                exit(EXIT_FAILURE);
            }
            return cpu;
        }
    }
    fprintf(stderr, "No available CPU\n");
    exit(EXIT_FAILURE);
}

static size_t parse_number(const char *text, size_t minimum, size_t maximum) {
    char *end;
    errno = 0;
    unsigned long long number = strtoull(text, &end, 10);
    if (errno || text[0] < '0' || text[0] > '9' || *end || number < minimum || number > maximum) {
        fprintf(stderr, "Expected an integer between %zu and %zu: %s\n", minimum, maximum, text);
        exit(EXIT_FAILURE);
    }
    return (size_t)number;
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--test")) {
        self_test();
        return 0;
    }
    if (argc < 2 || argc > 4) {
        fprintf(stderr, "Usage: %s --test\n       %s OUTPUT_DIRECTORY [REPEATS] [MAX_ROWS]\n", argv[0], argv[0]);
        return EXIT_FAILURE;
    }
    size_t repeats = argc > 2 ? parse_number(argv[2], 1, 100) : 7;
    size_t max_rows = argc > 3 ? parse_number(argv[3], 10000, 5000000) : 2000000;
    /* A fresh directory prevents accidental replacement of recorded results. */
    if (mkdir(argv[1], 0755)) {
        perror(argv[1]);
        return EXIT_FAILURE;
    }
    int cpu = pin_cpu();
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/timings.csv", argv[1]) >= (int)sizeof(path)) {
        fprintf(stderr, "Output path is too long\n");
        return EXIT_FAILURE;
    }
    FILE *output = fopen(path, "w");
    if (!output) { perror(path); return EXIT_FAILURE; }
    snprintf(path, sizeof(path), "%s/environment.txt", argv[1]);
    FILE *environment = fopen(path, "w");
    if (!environment) { perror(path); return EXIT_FAILURE; }
    fprintf(environment, "compiler=GCC %s\nbinary_bits=%zu\nrecord_bytes=%zu\nlogical_cpu=%d\n"
            "timer=CLOCK_MONOTONIC\nseed=42\nwarmup_runs=1\nrepeats=%zu\n",
            __VERSION__, 8 * sizeof(void *), sizeof(Record), cpu, repeats);
    fclose(environment);
    fprintf(output, "rows_left,rows_right,run,seed,matches,checksum,buckets,estimated_bytes,build_ms,probe_ms,total_ms\n");
    const size_t sizes[] = {10000, 100000, 500000, 1000000, 2000000, 5000000};
    for (size_t size_index = 0; size_index < sizeof(sizes) / sizeof(sizes[0]); ++size_index) {
        size_t count = sizes[size_index];
        if (count > max_rows) break;
        Record *left = allocate(count, sizeof(Record));
        Record *right = allocate(count, sizeof(Record));
        generate(left, right, count, 42);
        JoinResult expected = {(count + 1) / 2, 0};
        for (size_t i = 0; i < count; i += 2) expected.checksum += mix(((uint64_t)i << 32) | (i / 2));
        for (size_t run = 0; run <= repeats; ++run) {
            uint64_t start = now_ns();
            HashTable table = build(left, count);
            uint64_t built = now_ns();
            JoinResult result = probe(&table, right, count, NULL, 0);
            uint64_t finished = now_ns();
            if (result.matches != expected.matches || result.checksum != expected.checksum) {
                fprintf(stderr, "Generated-data result mismatch\n");
                return EXIT_FAILURE;
            }
            size_t bytes = 2 * count * sizeof(Record) + (count + table.bucket_count) * sizeof(size_t);
            if (run) {
                fprintf(output, "%zu,%zu,%zu,42,%" PRIu64 ",%" PRIu64 ",%zu,%zu,%.6f,%.6f,%.6f\n",
                        count, count, run, result.matches, result.checksum, table.bucket_count, bytes,
                        (built - start) / 1e6, (finished - built) / 1e6, (finished - start) / 1e6);
            }
            release(&table);
        }
        free(left);
        free(right);
        if (fflush(output)) { perror("Writing timings"); return EXIT_FAILURE; }
        printf("%zu rows per input: verified %" PRIu64 " matches; %zu recorded runs\n", count, expected.matches, repeats);
    }
    if (fclose(output)) { perror("Closing timings"); return EXIT_FAILURE; }
    return 0;
}
