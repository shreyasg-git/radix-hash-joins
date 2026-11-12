/* Shared pieces for the radix experiments.
 *
 * Uses the same record layout, hash function, input generator and timer as the
 * simple hash join (src/hash_join.c), so results can be compared with it.
 *
 * What is new compared with the simple hash join:
 *   - allocate_touched(): buffers are allocated and written once BEFORE any
 *     timer starts, so page faults are not mixed into the join timings.
 *     It can also ask Linux for 4 KiB or 2 MiB pages (huge pages).
 *   - Counters: optional hardware counters (cache and TLB misses) read with
 *     perf_event_open. Virtual machines such as WSL2 often do not expose them;
 *     then the columns are left empty instead of failing.
 */
#ifndef RADIX_COMMON_H
#define RADIX_COMMON_H

#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#ifdef __GLIBC__
#include <malloc.h>
#endif
#ifdef __APPLE__
#include <pthread.h>
#include <sys/qos.h>
#include <sys/sysctl.h>
#endif
#ifdef __linux__
#include <linux/perf_event.h>
#include <sched.h>
#include <sys/syscall.h>
#endif

/* A record is a 32-bit join key and the row number in its original table. */
typedef struct {
    uint32_t key;
    uint32_t row_id;
} Record;

static inline void die(const char *message) {
    fprintf(stderr, "%s\n", message);
    exit(EXIT_FAILURE);
}

static inline void *allocate(size_t count, size_t size) {
    if (size && count > SIZE_MAX / size) die("Allocation size overflow");
    void *memory = malloc(count ? count * size : 1);
    if (!memory) die("Out of memory");
    return memory;
}

/* Same 64-bit mixer as the simple hash join. A join built on radix partitioning
 * must take its bucket bits from ABOVE the radix bits (see radix_of). */
static inline uint64_t mix(uint64_t value) {
    value ^= value >> 30;
    value *= UINT64_C(0xbf58476d1ce4e5b9);
    value ^= value >> 27;
    value *= UINT64_C(0x94d049bb133111eb);
    return value ^ (value >> 31);
}

static inline uint64_t now_ns(void) {
#ifdef __APPLE__
    /* macOS: CLOCK_MONOTONIC only has microsecond resolution; this one has nanoseconds. */
    return clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
#endif
    struct timespec time;
    if (clock_gettime(CLOCK_MONOTONIC, &time)) die("clock_gettime failed");
    return (uint64_t)time.tv_sec * UINT64_C(1000000000) + (uint64_t)time.tv_nsec;
}

/* ---------- memory ---------------------------------------------------- */

typedef enum { PAGES_DEFAULT = 0, PAGES_4K = 1, PAGES_2M = 2 } PageMode;
__attribute__((unused)) static const char *page_mode_name[] = {"default", "4k", "2m"};

/* Allocate, optionally request a page size, and write every byte once so the
 * operating system maps all pages now and not inside a timed phase. */
static inline void *allocate_touched(size_t bytes, PageMode mode) {
    const size_t align = (size_t)2 << 20; /* 2 MiB, the x86 huge-page size */
    size_t rounded = (bytes + align - 1) / align * align;
    if (rounded == 0) rounded = align;
#ifdef __GLIBC__
    /* Always give large buffers a fresh mapping. Otherwise glibc may reuse heap
     * memory that was already mapped with small pages, and the page-size
     * request below would only partly take effect. */
    static int fixed_threshold = 0;
    if (!fixed_threshold) { mallopt(M_MMAP_THRESHOLD, 1 << 20); fixed_threshold = 1; }
#endif
    void *memory = NULL;
    if (posix_memalign(&memory, align, rounded)) die("Out of memory");
#if defined(MADV_HUGEPAGE) && defined(MADV_NOHUGEPAGE)
    if (mode == PAGES_2M) madvise(memory, rounded, MADV_HUGEPAGE);
    if (mode == PAGES_4K) madvise(memory, rounded, MADV_NOHUGEPAGE);
#else
    (void)mode;
#endif
    memset(memory, 0, rounded);
    return memory;
}

/* Kilobytes of this process currently backed by transparent huge pages.
 * Lets the report show whether the 2m mode really got huge pages. */
static inline long anon_huge_kb(void) {
#ifdef __linux__
    FILE *file = fopen("/proc/self/smaps_rollup", "r");
    if (!file) return -1;
    char line[256];
    long kb = -1;
    while (fgets(line, sizeof(line), file)) {
        if (sscanf(line, "AnonHugePages: %ld kB", &kb) == 1) break;
    }
    fclose(file);
    return kb;
#else
    return -1;
#endif
}

/* ---------- input data (identical to the simple hash join) -------------- */

static inline uint64_t random_next(uint64_t *state) {
    *state += UINT64_C(0x9e3779b97f4a7c15);
    return mix(*state);
}

static inline void shuffle(Record *records, size_t count, uint64_t *state) {
    for (size_t i = count; i > 1; --i) {
        size_t j = random_next(state) % i;
        Record temporary = records[i - 1];
        records[i - 1] = records[j];
        records[j] = temporary;
    }
}

/* Left keys 0..N-1, right keys 0,2,4..2(N-1): exactly ceil(N/2) matches. */
static inline void generate(Record *left, Record *right, size_t count, uint64_t seed) {
    for (size_t i = 0; i < count; ++i) {
        left[i] = (Record){(uint32_t)i, (uint32_t)i};
        right[i] = (Record){(uint32_t)(2 * i), (uint32_t)i};
    }
    shuffle(left, count, &seed);
    shuffle(right, count, &seed);
}

static inline uint64_t pair_hash(uint32_t left_row, uint32_t right_row) {
    return mix(((uint64_t)left_row << 32) | right_row);
}


/* ---------- radix partitioning ------------------------------------------
 * Radix value of a record = the lowest `total_bits` bits of mix(key).
 * With several passes, pass 1 splits on the TOP group of those bits, pass 2
 * splits every cluster from pass 1 on the next group, and so on. This is the
 * multi-pass radix-cluster of Boncz, Manegold and Kersten: each pass only
 * writes to 2^bits_this_pass places at a time, which keeps that number below
 * the TLB and cache-line limits even when the total number of clusters is large.
 */
static inline size_t radix_of(uint32_t key, unsigned shift, unsigned bits) {
    return (size_t)(mix(key) >> shift) & (((size_t)1 << bits) - 1);
}

/* Bits handled by each pass: as even as possible, earlier passes get the extra. */
static inline void split_bits(unsigned total_bits, unsigned passes, unsigned *bits) {
    for (unsigned p = 0; p < passes; ++p) bits[p] = total_bits / passes + (p < total_bits % passes);
}

/* One pass over every existing cluster.
 * Input: clusters c = 0..cluster_count-1 at src[start[c] .. start[c+1]).
 * Output: cluster_count * 2^bits clusters in dst, their bounds in new_start
 *         (length cluster_count * 2^bits + 1).
 * The pass is two sweeps so they can be timed separately:
 *   histogram: count how many records go to each sub-cluster;
 *   scatter:   copy each record to its sub-cluster's next free slot.
 * The histogram writes counts into new_start, which is then turned into
 * positions with a prefix sum; that sum is counted in the scatter time.
 * The output occupies the same index range as the input, dst[start[0] ..
 * start[cluster_count]), so the function also works on a single cluster. */
typedef struct {
    uint64_t histogram_ns;
    uint64_t scatter_ns;
} PassTime;

static inline PassTime radix_pass(const Record *src, Record *dst, const size_t *start, size_t cluster_count,
                           unsigned shift, unsigned bits, size_t *new_start) {
    const size_t fanout = (size_t)1 << bits;
    PassTime time;
    uint64_t t0 = now_ns();
    size_t total = cluster_count * fanout;
    for (size_t i = 0; i <= total; ++i) new_start[i] = 0;
    new_start[0] = start[0]; /* output starts where the input clusters start */
    for (size_t c = 0; c < cluster_count; ++c) {
        size_t *count = new_start + c * fanout + 1;
        for (size_t i = start[c]; i < start[c + 1]; ++i) ++count[radix_of(src[i].key, shift, bits)];
    }
    uint64_t t1 = now_ns();
    for (size_t i = 1; i <= total; ++i) new_start[i] += new_start[i - 1]; /* now cluster starts */
    for (size_t c = 0; c < cluster_count; ++c) {
        size_t *position = new_start + c * fanout; /* advanced while scattering, restored below */
        for (size_t i = start[c]; i < start[c + 1]; ++i) {
            size_t to = position[radix_of(src[i].key, shift, bits)]++;
            dst[to] = src[i];
        }
    }
    /* Each position[j] now equals the start of cluster j+1; shift back by one. */
    for (size_t i = total; i > 0; --i) new_start[i] = new_start[i - 1];
    new_start[0] = start[0];
    uint64_t t2 = now_ns();
    time.histogram_ns = t1 - t0;
    time.scatter_ns = t2 - t1;
    return time;
}

/* ---------- optional hardware counters --------------------------------- */

enum { EV_CYCLES, EV_INSTRUCTIONS, EV_L1D_MISS, EV_LLC_MISS, EV_DTLB_MISS, EV_PAGE_FAULTS, EV_COUNT };
__attribute__((unused)) static const char *event_name[EV_COUNT] = {"cycles", "instructions", "l1d_read_misses",
                                           "llc_read_misses", "dtlb_read_misses", "page_faults"};
typedef struct {
    int fd[EV_COUNT];
} Counters;
typedef struct {
    uint64_t value[EV_COUNT];
} CounterValues;

static inline void counters_open(Counters *counters) {
    for (int e = 0; e < EV_COUNT; ++e) counters->fd[e] = -1;
#ifdef __linux__
    const uint64_t cache_read_miss = (PERF_COUNT_HW_CACHE_OP_READ << 8) | (PERF_COUNT_HW_CACHE_RESULT_MISS << 16);
    struct { uint32_t type; uint64_t config; } spec[EV_COUNT] = {
        {PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES},
        {PERF_TYPE_HARDWARE, PERF_COUNT_HW_INSTRUCTIONS},
        {PERF_TYPE_HW_CACHE, PERF_COUNT_HW_CACHE_L1D | cache_read_miss},
        {PERF_TYPE_HW_CACHE, PERF_COUNT_HW_CACHE_LL | cache_read_miss},
        {PERF_TYPE_HW_CACHE, PERF_COUNT_HW_CACHE_DTLB | cache_read_miss},
        {PERF_TYPE_SOFTWARE, PERF_COUNT_SW_PAGE_FAULTS},
    };
    for (int e = 0; e < EV_COUNT; ++e) {
        struct perf_event_attr attr;
        memset(&attr, 0, sizeof(attr));
        attr.size = sizeof(attr);
        attr.type = spec[e].type;
        attr.config = spec[e].config;
        attr.exclude_kernel = 1;
        attr.exclude_hv = 1;
        counters->fd[e] = (int)syscall(SYS_perf_event_open, &attr, 0, -1, -1, 0);
    }
#endif
}

static inline CounterValues counters_read(const Counters *counters) {
    CounterValues values;
    for (int e = 0; e < EV_COUNT; ++e) {
        values.value[e] = UINT64_MAX;
        uint64_t value;
        if (counters->fd[e] >= 0 && read(counters->fd[e], &value, sizeof(value)) == (ssize_t)sizeof(value))
            values.value[e] = value;
    }
    return values;
}

/* Column names for the counters; prefix lets one row hold several phases. */
static inline void counters_header(FILE *file, const char *prefix) {
    for (int e = 0; e < EV_COUNT; ++e) fprintf(file, ",%s%s", prefix, event_name[e]);
}

/* Median cost of one now_ns() call, so reports can judge timer overhead. */
static inline double timer_cost_ns(void) {
    enum { SAMPLES = 2001 };
    static uint64_t cost[SAMPLES];
    for (int i = 0; i < SAMPLES; ++i) { uint64_t a = now_ns(); uint64_t b = now_ns(); cost[i] = b - a; }
    for (int i = 1; i < SAMPLES; ++i)  /* insertion sort: tiny array, outside any timing */
        for (int j = i; j > 0 && cost[j - 1] > cost[j]; --j) { uint64_t t = cost[j]; cost[j] = cost[j - 1]; cost[j - 1] = t; }
    return (double)cost[SAMPLES / 2];
}

/* Writes after - before for every counter, or an empty field if unavailable. */
static inline void counters_write(FILE *file, CounterValues before, CounterValues after) {
    for (int e = 0; e < EV_COUNT; ++e) {
        if (before.value[e] == UINT64_MAX || after.value[e] == UINT64_MAX) fputs(",", file);
        else fprintf(file, ",%" PRIu64, after.value[e] - before.value[e]);
    }
}

static inline void counters_describe(FILE *file, const Counters *counters) {
    fputs("counters_available=", file);
    int any = 0;
    for (int e = 0; e < EV_COUNT; ++e) {
        if (counters->fd[e] >= 0) { fprintf(file, "%s%s", any ? ";" : "", event_name[e]); any = 1; }
    }
    fputs(any ? "\n" : "none\n", file);
}

/* ---------- run order and VM interference ------------------------------- */

/* Shuffles 0..count-1 into a new order for every round of measurements, so no
 * setting always runs right after the same other setting. Fixed seed, so the
 * order is repeatable. */
static inline void shuffle_order(size_t *order, size_t count, uint64_t *state) {
    for (size_t i = 0; i < count; ++i) order[i] = i;
    for (size_t i = count; i > 1; --i) {
        size_t j = random_next(state) % i;
        size_t t = order[i - 1]; order[i - 1] = order[j]; order[j] = t;
    }
}

/* CPU time the hypervisor took away from us ("steal" in /proc/stat), in ms.
 * cpu < 0 sums all CPUs. Returns -1 when not available (not Linux). On a real
 * laptop it stays 0; on a busy virtual machine it shows when other tenants
 * interrupted a measurement. */
static inline double steal_ms(int cpu) {
#ifdef __linux__
    FILE *file = fopen("/proc/stat", "r");
    if (!file) return -1;
    char line[512], want[32];
    if (cpu < 0) snprintf(want, sizeof(want), "cpu ");
    else snprintf(want, sizeof(want), "cpu%d ", cpu);
    double result = -1;
    while (fgets(line, sizeof(line), file)) {
        if (strncmp(line, want, strlen(want))) continue;
        unsigned long long v[8] = {0};
        if (sscanf(line + strlen(want), "%llu %llu %llu %llu %llu %llu %llu %llu",
                   &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6], &v[7]) == 8)
            result = 1000.0 * (double)v[7] / (double)sysconf(_SC_CLK_TCK);
        break;
    }
    fclose(file);
    return result;
#else
    (void)cpu;
    return -1;
#endif
}

static inline double steal_delta(double before, double after) {
    return (before < 0 || after < 0) ? -1 : after - before;
}

/* ---------- small utilities -------------------------------------------- */

static inline int pin_to_cpu(int index) {
#ifdef __linux__
    cpu_set_t available, selected;
    if (sched_getaffinity(0, sizeof(available), &available)) return -1;
    int seen = 0, count = CPU_COUNT(&available);
    for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu) {
        if (!CPU_ISSET(cpu, &available)) continue;
        if (seen++ == index % count) {
            CPU_ZERO(&selected);
            CPU_SET(cpu, &selected);
            if (sched_setaffinity(0, sizeof(selected), &selected)) return -1;
            return cpu;
        }
    }
#elif defined(__APPLE__)
    /* macOS cannot pin a thread to a CPU. The highest quality-of-service class
     * asks the scheduler to prefer the performance cores. */
    (void)index;
    pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0);
#else
    (void)index;
#endif
    return -1;
}

static inline size_t parse_number(const char *text, size_t minimum, size_t maximum) {
    char *end;
    errno = 0;
    unsigned long long number = strtoull(text, &end, 10);
    if (errno || text[0] < '0' || text[0] > '9' || *end || number < minimum || number > maximum) {
        fprintf(stderr, "Expected an integer between %zu and %zu: %s\n", minimum, maximum, text);
        exit(EXIT_FAILURE);
    }
    return (size_t)number;
}

/* Parses "1,2,4" into values; returns how many were read. */
static inline size_t parse_list(const char *text, size_t *values, size_t capacity, size_t minimum, size_t maximum) {
    char buffer[1024];
    if (strlen(text) >= sizeof(buffer)) die("List argument is too long");
    strcpy(buffer, text);
    size_t count = 0;
    for (char *item = strtok(buffer, ","); item; item = strtok(NULL, ",")) {
        if (count == capacity) die("Too many list items");
        values[count++] = parse_number(item, minimum, maximum);
    }
    if (!count) die("Empty list argument");
    return count;
}

/* Creates a NEW results folder (never overwrites earlier results). */
static inline FILE *open_in(const char *directory, const char *name) {
    char path[4096];
    if (snprintf(path, sizeof(path), "%s/%s", directory, name) >= (int)sizeof(path)) die("Path too long");
    FILE *file = fopen(path, "w");
    if (!file) { perror(path); exit(EXIT_FAILURE); }
    return file;
}

static inline void make_results_directory(const char *directory) {
    if (mkdir(directory, 0755)) {
        perror(directory);
        fprintf(stderr, "Choose a new folder name; earlier results are never overwritten.\n");
        exit(EXIT_FAILURE);
    }
}

static inline void write_common_environment(FILE *file) {
#ifdef __APPLE__
    fputs("timer=CLOCK_UPTIME_RAW (nanoseconds)\n", file);
#else
    fputs("timer=CLOCK_MONOTONIC\n", file);
#endif
    fprintf(file, "compiler=%s\nbinary_bits=%zu\nrecord_bytes=%zu\n",
            __VERSION__, 8 * sizeof(void *), sizeof(Record));
    fprintf(file, "online_cpus=%ld\npage_bytes=%ld\n", sysconf(_SC_NPROCESSORS_ONLN), sysconf(_SC_PAGESIZE));
#ifdef __linux__
    for (int index = 0; index < 8; ++index) {
        char path[256], level[32] = "", type[32] = "", size[32] = "";
        snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu0/cache/index%d/level", index);
        FILE *f = fopen(path, "r");
        if (!f) break;
        if (!fscanf(f, "%31s", level)) level[0] = 0;
        fclose(f);
        snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu0/cache/index%d/type", index);
        if ((f = fopen(path, "r"))) { if (!fscanf(f, "%31s", type)) type[0] = 0; fclose(f); }
        snprintf(path, sizeof(path), "/sys/devices/system/cpu/cpu0/cache/index%d/size", index);
        if ((f = fopen(path, "r"))) { if (!fscanf(f, "%31s", size)) size[0] = 0; fclose(f); }
        fprintf(file, "cache_L%s_%s=%s\n", level, type, size);
    }
    FILE *thp = fopen("/sys/kernel/mm/transparent_hugepage/enabled", "r");
    if (thp) {
        char line[128] = "";
        if (fgets(line, sizeof(line), thp)) fprintf(file, "transparent_hugepage=%s", line);
        fclose(thp);
    }
#elif defined(__APPLE__)
    /* Cache sizes of the performance cores (perflevel0) when the CPU has them. */
    const char *names[][2] = {{"hw.perflevel0.l1dcachesize", "cache_L1_Data"},
                              {"hw.perflevel0.l2cachesize", "cache_L2_Unified"},
                              {"hw.l1dcachesize", "cache_L1_Data"}, {"hw.l2cachesize", "cache_L2_Unified"},
                              {"hw.l3cachesize", "cache_L3_Unified"}};
    int have_l1 = 0, have_l2 = 0;
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        int64_t value = 0;
        size_t length = sizeof(value);
        if (sysctlbyname(names[i][0], &value, &length, NULL, 0) || value <= 0) continue;
        int is_l1 = strstr(names[i][1], "L1") != NULL, is_l2 = strstr(names[i][1], "L2") != NULL;
        if ((is_l1 && have_l1) || (is_l2 && have_l2)) continue;
        have_l1 |= is_l1;
        have_l2 |= is_l2;
        fprintf(file, "%s=%lldK\n", names[i][1], (long long)(value / 1024));
    }
    char brand[256] = "";
    size_t length = sizeof(brand);
    if (!sysctlbyname("machdep.cpu.brand_string", brand, &length, NULL, 0)) fprintf(file, "cpu_model=%s\n", brand);
    fputs("thread_pinning=not supported on macOS; threads use the highest QoS class (prefers performance cores)\n",
          file);
#endif
}

#endif
