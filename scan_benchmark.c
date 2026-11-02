/* Windows and Linux x86/x64 stride scan. Cache preparation is outside the timer.
   gcc -O2 -std=c11 -Wall -Wextra -msse2 scan_benchmark.c -o scan_benchmark.exe
   scan_benchmark.exe [output-directory] [repetitions]
   Default output directory is results, repetitions 30.
   CLFLUSH invalidates target lines; hardware may prefetch them during scanning.
   Volatile byte loads deliberately keep the scalar access pattern observable.
*/
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <direct.h>
#else
#define _GNU_SOURCE
#include <sched.h>
#include <sys/stat.h>
#include <time.h>
#endif
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <cpuid.h>
#include <emmintrin.h>

#define READS 200000u
#define BUFFER_BYTES (256u * 1024u * 1024u)
#define NSTRIDES 11
static const unsigned strides[NSTRIDES] = {1,2,4,8,16,32,64,128,256,512,1024};
static volatile uint64_t result_sink;
static uint32_t rng = 20261105u;
static uint32_t random_u32(void) {
    rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5; return rng;
}
static uint64_t scan(const volatile unsigned char *data, unsigned stride) {
    uint32_t sum = 0; /* At most 200000 * 255 = 51000000; fits in 32 bits. */
    const volatile unsigned char *p = data;
    for (unsigned i = 0; i < READS; ++i, p += stride) sum += *p;
    return sum;
}
static void flush_selected(const unsigned char *data, unsigned stride, unsigned line) {
    size_t last = (size_t)(READS - 1) * stride;
    if (stride <= line) {
        for (size_t p = 0; p <= last; p += line) _mm_clflush(data + p);
    } else {
        for (unsigned i = 0; i < READS; ++i) _mm_clflush(data + (size_t)i * stride);
    }
    _mm_mfence();
}
static FILE *open_output(const char *dir, const char *name) {
    char path[2048]; snprintf(path, sizeof(path), "%s/%s", dir, name);
    FILE *f = fopen(path, "w");
    if (!f) { perror(path); exit(2); } return f;
}

#ifdef _WIN32
typedef LARGE_INTEGER TimerStamp;
static TimerStamp timer_now(void) {
    TimerStamp stamp;
    QueryPerformanceCounter(&stamp);
    return stamp;
}
static double elapsed_ns(TimerStamp start, TimerStamp end, long long frequency) {
    return 1e9 * (end.QuadPart - start.QuadPart) / frequency;
}
#else
typedef struct timespec TimerStamp;
static TimerStamp timer_now(void) {
    TimerStamp stamp;
    if (clock_gettime(CLOCK_MONOTONIC, &stamp) != 0) {
        perror("clock_gettime");
        exit(2);
    }
    return stamp;
}
static double elapsed_ns(TimerStamp start, TimerStamp end, long long frequency) {
    (void)frequency;
    return 1e9 * (end.tv_sec - start.tv_sec) + (end.tv_nsec - start.tv_nsec);
}
#endif

int main(int argc, char **argv) {
    const char *dir = argc > 1 ? argv[1] : "results";
    int reps = argc > 2 ? atoi(argv[2]) : 30;
    if (reps < 5 || reps > 1000) { fprintf(stderr,"Repetitions must be 5..1000\n"); return 2; }
#ifdef _WIN32
    int mkdir_status = _mkdir(dir);
#else
    int mkdir_status = mkdir(dir, 0755);
#endif
    if (mkdir_status != 0 && errno != EEXIST) { perror(dir); return 2; }
    unsigned a,b,c,d;
    __cpuid(1,a,b,c,d);
    unsigned line = ((b >> 8) & 255u) * 8u;
    if (!(d & (1u<<19)) || !line) { fprintf(stderr,"CLFLUSH unavailable\n"); return 2; }
#ifdef _WIN32
    DWORD_PTR process_mask, system_mask;
    if (!GetProcessAffinityMask(GetCurrentProcess(), &process_mask, &system_mask)) return 2;
    DWORD_PTR selected = process_mask & (~process_mask + 1);
    if (!SetThreadAffinityMask(GetCurrentThread(), selected)) { fprintf(stderr,"Affinity failed\n"); return 2; }
    LARGE_INTEGER freq;
    QueryPerformanceFrequency(&freq);
    long long frequency = freq.QuadPart;
    unsigned char *data = (unsigned char *)VirtualAlloc(NULL,BUFFER_BYTES,MEM_RESERVE|MEM_COMMIT,PAGE_READWRITE);
#else
    cpu_set_t available, pinned;
    if (sched_getaffinity(0, sizeof(available), &available) != 0) {
        perror("sched_getaffinity");
        return 2;
    }
    int cpu = 0;
    while (cpu < CPU_SETSIZE && !CPU_ISSET(cpu, &available)) ++cpu;
    if (cpu == CPU_SETSIZE) { fprintf(stderr, "No available CPU\n"); return 2; }
    CPU_ZERO(&pinned);
    CPU_SET(cpu, &pinned);
    if (sched_setaffinity(0, sizeof(pinned), &pinned) != 0) {
        perror("sched_setaffinity");
        return 2;
    }
    const long long frequency = 1000000000LL;
    unsigned char *data = aligned_alloc(line, BUFFER_BYTES);
#endif
    if (!data) { fprintf(stderr,"Allocation failed\n"); return 2; }
    for (size_t i=0; i<BUFFER_BYTES; ++i) data[i]=(unsigned char)random_u32();
    uint64_t expected[NSTRIDES];
    for (int s=0;s<NSTRIDES;++s) expected[s]=scan(data,strides[s]);
    FILE *meta = open_output(dir,"benchmark_metadata.json");
#ifdef _WIN32
    SYSTEMTIME utc; GetSystemTime(&utc);
    SYSTEM_POWER_STATUS power; int power_ok=GetSystemPowerStatus(&power);
    fprintf(meta,"{\n  \"reads_per_scan\": %u,\n  \"buffer_bytes\": %u,\n  \"repetitions\": %d,\n  \"warmup_rounds\": 3,\n  \"line_bytes_cpuid\": %u,\n  \"qpc_frequency_hz\": %lld,\n  \"affinity_mask\": %llu,\n  \"binary_bits\": %u,\n  \"compiler\": \"GCC %s\",\n  \"flags\": \"-O2 -std=c11 -Wall -Wextra -msse2\",\n  \"shuffle_seed\": 20261105,\n  \"ac_line_status\": %d,\n  \"battery_percent\": %d,\n  \"utc_started\": \"%04u-%02u-%02uT%02u:%02u:%02uZ\"\n}\n",
        READS,BUFFER_BYTES,reps,line,frequency,(unsigned long long)selected,
        (unsigned)(8*sizeof(void*)),__VERSION__,power_ok?power.ACLineStatus:-1,power_ok?power.BatteryLifePercent:-1,
        utc.wYear,utc.wMonth,utc.wDay,utc.wHour,utc.wMinute,utc.wSecond);
#else
    fprintf(meta, "{\n  \"platform\": \"Linux\",\n  \"timer\": \"CLOCK_MONOTONIC\",\n"
                  "  \"reads_per_scan\": %u,\n  \"buffer_bytes\": %u,\n  \"repetitions\": %d,\n"
                  "  \"warmup_rounds\": 3,\n  \"line_bytes_cpuid\": %u,\n  \"logical_cpu\": %d,\n"
                  "  \"binary_bits\": %u,\n  \"compiler\": \"GCC %s\",\n  \"shuffle_seed\": 20261105\n}\n",
            READS, BUFFER_BYTES, reps, line, cpu, (unsigned)(8 * sizeof(void *)), __VERSION__);
#endif
    fclose(meta);
    FILE *timer=open_output(dir,"timer_overhead.csv");
    fprintf(timer,"sample,elapsed_ns\n");
    for (int i = 0; i < 1000; ++i) {
        TimerStamp t0 = timer_now();
        TimerStamp t1 = timer_now();
        fprintf(timer, "%d,%.3f\n", i, elapsed_ns(t0, t1, frequency));
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
            if(repeat) result_sink=scan(data,stride); else flush_selected(data,stride,line);
            _mm_lfence(); TimerStamp t0 = timer_now();
            uint64_t checksum=scan(data,stride);
            _mm_lfence(); TimerStamp t1 = timer_now(); result_sink=checksum;
            if(checksum!=expected[s]){fprintf(stderr,"Checksum mismatch\n");return 3;}
            if(round>=0){
                size_t span=(size_t)(READS-1)*stride+1;
                size_t lines=stride<line?(span+line-1)/line:READS;
                fprintf(out,"%d,%d,%s,%u,%u,%llu,%llu,%.3f,%llu\n",round+1,j+1,
                    repeat?"repeat_scan":"flushed",stride,READS,(unsigned long long)span,
                    (unsigned long long)lines,elapsed_ns(t0,t1,frequency),
                    (unsigned long long)checksum);
            }
        }
        fflush(out);
        if(round>=0 && (round+1)%5==0)printf("Completed %d/%d rounds\n",round+1,reps);
    }
    fclose(out);
#ifdef _WIN32
    VirtualFree(data,0,MEM_RELEASE);
#else
    free(data);
#endif
    printf("All checksums verified; output: %s; cache line: %u bytes\n",dir,line);
    return 0;
}
