/* M5 Pro (macOS, Apple silicon) version of src/scan_benchmark.c.
 *
 *   cc -O2 -std=c11 -Wall -Wextra scan_benchmark.c -o scan_benchmark
 *   ./scan_benchmark OUTPUT_DIRECTORY [repetitions]      (default 30)
 *
 * Same experiment as the original: 200,000 one-byte reads per scan, strides
 * 1..1024 bytes, a "flushed" and an "immediate repeat" scan, 3 warmup rounds,
 * then 30 recorded rounds in a new shuffled order each round, same random
 * fill, same seeds, same CSV columns. What changes, and why:
 *
 *   1. Emptying the cache. The original used the x86 CLFLUSH instruction. Its
 *      ARM counterpart (DC CIVAC) runs on macOS but has no measurable effect
 *      for a normal program: random reads of 512 KiB took 2.6 ns each before
 *      and after it. So before each "flushed" scan this program reads a
 *      separate 512 MiB buffer, far larger than every cache on the chip, which
 *      pushes the scan's data out. The check is repeated at start-up and saved
 *      in benchmark_metadata.json (eviction_check_*). Unlike CLFLUSH, this also
 *      empties the address-translation cache (TLB) for the scan's pages.
 *   2. Cache line size from sysctl hw.cachelinesize (128 bytes on the M5)
 *      instead of CPUID. It is only used for the selected_cache_lines column.
 *   3. Timer: CLOCK_UPTIME_RAW (41.7 ns ticks) instead of CLOCK_MONOTONIC.
 *   4. No thread pinning on macOS; the thread asks for the highest
 *      quality-of-service class, which runs on the performance cores.
 *   5. Ordering barriers: DSB + ISB instead of LFENCE/MFENCE.
 */
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <time.h>

#if !defined(__APPLE__) || !defined(__aarch64__)
#error "This copy is for macOS on Apple silicon. Use ../src/scan_benchmark.c on x86."
#endif

#define READS 200000u
#define BUFFER_BYTES (256u * 1024u * 1024u)
#define EVICT_BYTES ((size_t)512u * 1024u * 1024u)
#define NSTRIDES 11
static const unsigned strides[NSTRIDES] = {1,2,4,8,16,32,64,128,256,512,1024};
static volatile uint64_t result_sink;
static uint32_t rng = 20261105u;
static uint32_t random_u32(void) {
    rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5; return rng;
}
static inline void barrier(void) { __asm__ volatile("dsb sy\n\tisb" ::: "memory"); }
static inline uint64_t timer_now(void) { return clock_gettime_nsec_np(CLOCK_UPTIME_RAW); }

static uint64_t scan(const volatile unsigned char *data, unsigned stride) {
    uint32_t sum = 0; /* At most 200000 * 255 = 51000000; fits in 32 bits. */
    const volatile unsigned char *p = data;
    for (unsigned i = 0; i < READS; ++i, p += stride) sum += *p;
    return sum;
}

/* Read one byte of every cache line of a 512 MiB buffer. */
static unsigned char *evict_buffer;
static size_t line_bytes;
static void evict_caches(void) {
    const volatile unsigned char *e = evict_buffer;
    uint32_t sum = 0;
    for (size_t p = 0; p < EVICT_BYTES; p += line_bytes) sum += e[p];
    result_sink = sum;
    barrier();
}

static FILE *open_output(const char *dir, const char *name) {
    char path[2048]; snprintf(path, sizeof(path), "%s/%s", dir, name);
    FILE *f = fopen(path, "w");
    if (!f) { perror(path); exit(2); } return f;
}

/* Start-up check: dependent random reads over 4096 lines (512 KiB), timed
 * (a) right after reading them, (b) after DC CIVAC on every line, (c) after
 * reading the 512 MiB eviction buffer. Returns ns per read, best of 5. */
static void eviction_check(double *cached, double *after_dc_civac, double *after_evict) {
    const size_t lines = 4096, step = line_bytes / sizeof(size_t);
    size_t *chain = aligned_alloc(16384, lines * line_bytes);
    size_t *order = malloc(lines * sizeof(size_t));
    if (!chain || !order) { fprintf(stderr, "Allocation failed\n"); exit(2); }
    for (size_t i = 0; i < lines; ++i) order[i] = i;
    uint64_t s = 12345;
    for (size_t i = lines - 1; i > 0; --i) {
        s ^= s << 13; s ^= s >> 7; s ^= s << 17;
        size_t j = s % (i + 1), t = order[i]; order[i] = order[j]; order[j] = t;
    }
    for (size_t i = 0; i < lines; ++i) chain[order[i] * step] = order[(i + 1) % lines] * step;
    *cached = *after_dc_civac = *after_evict = 1e9;
    for (int rep = 0; rep < 5; ++rep) {
        size_t p = 0;
        for (size_t i = 0; i < lines; ++i) p = chain[p];
        result_sink = p;
        double t[3];
        for (int mode = 0; mode < 3; ++mode) {
            if (mode == 1) {
                for (size_t i = 0; i < lines; ++i) __asm__ volatile("dc civac, %0" :: "r"(chain + i * step) : "memory");
                barrier();
            } else if (mode == 2) {
                evict_caches();
            }
            uint64_t t0 = timer_now();
            p = 0;
            for (size_t i = 0; i < lines; ++i) p = chain[p];
            uint64_t t1 = timer_now();
            result_sink = p;
            t[mode] = (double)(t1 - t0) / lines;
            /* re-warm before the next mode */
            p = 0; for (size_t i = 0; i < lines; ++i) p = chain[p]; result_sink = p;
        }
        if (t[0] < *cached) *cached = t[0];
        if (t[1] < *after_dc_civac) *after_dc_civac = t[1];
        if (t[2] < *after_evict) *after_evict = t[2];
    }
    free(chain); free(order);
}

int main(int argc, char **argv) {
    const char *dir = argc > 1 ? argv[1] : "results";
    int reps = argc > 2 ? atoi(argv[2]) : 30;
    if (reps < 5 || reps > 1000) { fprintf(stderr,"Repetitions must be 5..1000\n"); return 2; }
    if (mkdir(dir, 0755) != 0 && errno != EEXIST) { perror(dir); return 2; }
    if (pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)) { fprintf(stderr,"QoS failed\n"); return 2; }
    size_t size = sizeof(line_bytes);
    if (sysctlbyname("hw.cachelinesize", &line_bytes, &size, NULL, 0) || !line_bytes) {
        fprintf(stderr,"Could not read hw.cachelinesize\n"); return 2;
    }
    unsigned line = (unsigned)line_bytes;
    unsigned char *data = aligned_alloc(16384, BUFFER_BYTES);
    evict_buffer = aligned_alloc(16384, EVICT_BYTES);
    if (!data || !evict_buffer) { fprintf(stderr,"Allocation failed\n"); return 2; }
    for (size_t i=0; i<BUFFER_BYTES; ++i) data[i]=(unsigned char)random_u32();
    for (size_t i=0; i<EVICT_BYTES; ++i) evict_buffer[i]=(unsigned char)i;
    uint64_t expected[NSTRIDES];
    for (int s=0;s<NSTRIDES;++s) expected[s]=scan(data,strides[s]);

    double cached, after_dc_civac, after_evict;
    eviction_check(&cached, &after_dc_civac, &after_evict);
    char cpu[256] = "unknown"; size = sizeof(cpu);
    sysctlbyname("machdep.cpu.brand_string", cpu, &size, NULL, 0);
    FILE *meta = open_output(dir,"benchmark_metadata.json");
    fprintf(meta, "{\n  \"platform\": \"macOS\",\n  \"cpu\": \"%s\",\n  \"timer\": \"CLOCK_UPTIME_RAW\",\n"
                  "  \"reads_per_scan\": %u,\n  \"buffer_bytes\": %u,\n  \"repetitions\": %d,\n"
                  "  \"warmup_rounds\": 3,\n  \"line_bytes_sysctl\": %u,\n  \"logical_cpu\": -1,\n"
                  "  \"thread_placement\": \"highest QoS class (performance cores); macOS cannot pin threads\",\n"
                  "  \"flush_method\": \"read every cache line of a separate %zu MiB buffer\",\n"
                  "  \"eviction_check_cached_ns_per_read\": %.2f,\n"
                  "  \"eviction_check_after_dc_civac_ns_per_read\": %.2f,\n"
                  "  \"eviction_check_after_eviction_ns_per_read\": %.2f,\n"
                  "  \"binary_bits\": %u,\n  \"compiler\": \"%s\",\n  \"shuffle_seed\": 20261105\n}\n",
            cpu, READS, BUFFER_BYTES, reps, line, EVICT_BYTES >> 20, cached, after_dc_civac, after_evict,
            (unsigned)(8 * sizeof(void *)), __VERSION__);
    fclose(meta);
    printf("Eviction check (ns per random read): cached %.2f, after DC CIVAC %.2f, after eviction %.2f\n",
           cached, after_dc_civac, after_evict);

    FILE *timer=open_output(dir,"timer_overhead.csv");
    fprintf(timer,"sample,elapsed_ns\n");
    for (int i = 0; i < 1000; ++i) {
        uint64_t t0 = timer_now();
        uint64_t t1 = timer_now();
        fprintf(timer, "%d,%.3f\n", i, (double)(t1 - t0));
    }
    fclose(timer);
    FILE *out=open_output(dir,"raw_timings.csv");
    fprintf(out,"round,order,condition,stride_bytes,reads,address_span_bytes,selected_cache_lines,elapsed_ns,checksum\n");
    rng=20261105u;
    for(int round=-3;round<reps;++round){
        int jobs[2*NSTRIDES]; for(int j=0;j<2*NSTRIDES;++j)jobs[j]=j;
        for(int j=2*NSTRIDES-1;j>0;--j){int k=random_u32()%(j+1);int tmp=jobs[j];jobs[j]=jobs[k];jobs[k]=tmp;}
        for(int j=0;j<2*NSTRIDES;++j){
            int s=jobs[j]%NSTRIDES, repeat=jobs[j]/NSTRIDES; unsigned stride=strides[s];
            if(repeat) result_sink=scan(data,stride); else evict_caches();
            barrier(); uint64_t t0 = timer_now();
            uint64_t checksum=scan(data,stride);
            barrier(); uint64_t t1 = timer_now(); result_sink=checksum;
            if(checksum!=expected[s]){fprintf(stderr,"Checksum mismatch\n");return 3;}
            if(round>=0){
                size_t span=(size_t)(READS-1)*stride+1;
                size_t lines=stride<line?(span+line-1)/line:READS;
                fprintf(out,"%d,%d,%s,%u,%u,%llu,%llu,%.3f,%llu\n",round+1,j+1,
                    repeat?"repeat_scan":"flushed",stride,READS,(unsigned long long)span,
                    (unsigned long long)lines,(double)(t1-t0),
                    (unsigned long long)checksum);
            }
        }
        fflush(out);
        if(round>=0 && (round+1)%5==0)printf("Completed %d/%d rounds\n",round+1,reps);
    }
    fclose(out);
    free(data); free(evict_buffer);
    printf("All checksums verified; output: %s; cache line: %u bytes\n",dir,line);
    return 0;
}
