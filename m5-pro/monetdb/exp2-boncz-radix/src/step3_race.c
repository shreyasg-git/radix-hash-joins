/* Experiment 2, step 3: Boncz's join against the tuner's join at 4 sizes,
 * measured like the race (../../../tuner/paper-race/src/race.c).
 *
 * plan.csv: rows_per_input,label,bits,passes,rows_per_bucket. A label that
 * starts with "boncz" is Boncz's join at those bits and passes (its own
 * rows-per-box rule; rows_per_bucket is ignored). Any other label is the
 * tuner's join at that setting. Per size: 1 untimed warm-up per setting, then
 * ROUNDS rounds, each running every setting once in a newly shuffled order.
 * Every answer is checked; a wrong one stops the run.
 *
 *   ./bin/step3 PLAN_CSV OUT_DIR ROUNDS | --machine-tag
 */
#define main tuner_main
#include "../../../tuner/src/tune.c"
#undef main

#include "boncz_radix.h"

#define MAX_PLAN 64

typedef struct {
    size_t n;
    char label[32];
    Knobs k;
    int boncz;
    double sum;
    size_t counted; /* Boncz cuts that had to count, over the timed joins */
} Entry;

static int read_plan(const char *path, Entry *e) {
    FILE *f = fopen(path, "r");
    if (!f) die("cannot read the plan");
    char line[256];
    int count = 0;
    if (!fgets(line, sizeof line, f)) die("the plan is empty");
    while (fgets(line, sizeof line, f)) {
        if (count == MAX_PLAN) die("too many settings in the plan");
        Entry x;
        memset(&x, 0, sizeof x);
        unsigned long long n;
        if (sscanf(line, "%llu,%31[^,],%u,%u,%lf", &n, x.label, &x.k.bits, &x.k.passes, &x.k.load) != 5)
            die("a plan line does not have 5 fields");
        x.n = (size_t)n;
        x.boncz = !strncmp(x.label, "boncz", 5);
        if (x.k.bits > max_bits_for(x.n) || x.k.passes > x.k.bits || (x.k.bits && !x.k.passes) ||
            (!x.boncz && x.k.load < LOADS[0] - 1e-12) || x.k.passes > BZ_MAX_PASSES)
            die("a plan setting is outside what the joins support");
        e[count++] = x;
    }
    fclose(f);
    if (!count) die("the plan has no settings");
    return count;
}

static double boncz_measure(Work *w, Knobs k) {
    const uint64_t t0 = now_ns();
    const Result got = bz_run_join(w, k.bits, k.passes, 0);
    const uint64_t t1 = now_ns();
    if (got.matches != w->expected.matches || got.checksum != w->expected.checksum) {
        fprintf(stderr, "step3: Boncz's join gave a wrong answer at %zu rows, %u bits, %u passes\n", w->n, k.bits,
                k.passes);
        exit(EXIT_FAILURE);
    }
    return (double)(t1 - t0) / (double)w->n;
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--machine-tag")) {
        char tag[64];
        machine_tag(tag, sizeof tag);
        printf("%s\n", tag);
        return 0;
    }
    if (argc != 4) {
        fprintf(stderr, "usage: %s PLAN_CSV OUT_DIR ROUNDS | --machine-tag\n", argv[0]);
        return 2;
    }
    const int rounds = atoi(argv[3]);
    if (rounds < 1 || rounds > 100) die("ROUNDS must be 1 to 100");
    static Entry plan[MAX_PLAN];
    const int np = read_plan(argv[1], plan);
    const char *dir = argv[2];
    if (mkdir(dir, 0755) && errno != EEXIST) die("cannot create the output folder");

    char cpu[256], prio[128], a1[96], seconds[512] = "";
    cpu_name(cpu, sizeof cpu);
    set_priority(prio, sizeof prio);
    struct utsname u;
    uname(&u);
    printf("Boncz's join (MonetDB 2007) against the tuner's join, on %s (%s %s)\n", cpu, u.sysname, u.release);
    printf("Thread: %s. Every setting: 1 untimed warm-up, then %d rounds in shuffled order.\n", prio, rounds);

    FILE *trials = open_csv(dir, "trials.csv",
                            "rows_per_input,label,bits,passes,rows_per_bucket,round,position,ns_per_row,boncz_cuts_counted");
    const uint64_t t_start = now_ns();
    uint64_t state = SEED;
    for (int i = 0; i < np;) {
        const size_t n = plan[i].n;
        int j = i;
        while (j < np && plan[j].n == n) ++j;
        const int m = j - i;
        const uint64_t t0 = now_ns();
        Work w = work_new(n);
        generate(w.r, w.s, n, SEED);
        w.expected = expected_result(n);
        printf("\n== %zu rows per input: %d settings x %d rounds\n", n, m, rounds);
        for (int k = i; k < j; ++k) { /* warm-up, not recorded */
            if (plan[k].boncz) boncz_measure(&w, plan[k].k);
            else measure(&w, plan[k].k);
        }
        int order[MAX_PLAN];
        for (int r = 0; r < rounds; ++r) {
            for (int k = 0; k < m; ++k) order[k] = i + k;
            for (int k = m; k > 1; --k) {
                int x = (int)(rng_next(&state) % (uint64_t)k);
                int t = order[k - 1]; order[k - 1] = order[x]; order[x] = t;
            }
            for (int pos = 0; pos < m; ++pos) {
                Entry *e = &plan[order[pos]];
                if (e->boncz) {
                    const size_t c0 = bz_counted;
                    const double ns = boncz_measure(&w, e->k);
                    e->sum += ns;
                    e->counted += bz_counted - c0;
                    fprintf(trials, "%zu,%s,%u,%u,,%d,%d,%.4f,%zu\n", n, e->label, e->k.bits, e->k.passes, r + 1,
                            pos + 1, ns, bz_counted - c0);
                } else {
                    const double ns = measure(&w, e->k);
                    e->sum += ns;
                    fprintf(trials, "%zu,%s,%u,%u,%g,%d,%d,%.4f,\n", n, e->label, e->k.bits, e->k.passes, e->k.load,
                            r + 1, pos + 1, ns);
                }
            }
        }
        fflush(trials);
        work_free(&w);
        const double s = (double)(now_ns() - t0) / 1e9;
        for (int k = i; k < j; ++k) {
            if (plan[k].boncz)
                snprintf(a1, sizeof a1, "Boncz: %u bits, %u pass%s", plan[k].k.bits, plan[k].k.passes,
                         plan[k].k.passes == 1 ? "" : "es");
            else
                knob_text(plan[k].k, a1, sizeof a1);
            printf("  %-12s %-40s average %6.2f ns per row\n", plan[k].label, a1, plan[k].sum / rounds);
        }
        printf("  %.1f s for this size\n", s);
        snprintf(seconds + strlen(seconds), sizeof seconds - strlen(seconds), "seconds_%zu=%.1f\n", n, s);
        i = j;
    }
    fclose(trials);

    const double total = (double)(now_ns() - t_start) / 1e9;
    char path[1024], date[64];
    snprintf(path, sizeof path, "%s/machine.txt", dir);
    FILE *mf = fopen(path, "w");
    if (!mf) die("cannot write machine.txt");
    const time_t now = time(NULL);
    strftime(date, sizeof date, "%Y-%m-%d %H:%M:%S", localtime(&now));
    fprintf(mf, "tuner_spec=%d\nboncz_source=MonetDB5/src/modules/mal/radix.mx at 33a91903a7 (2007-09-12)\n"
                "date=%s\ncpu=%s\nos=%s %s %s\nlogical_cpus=%ld\nram_bytes=%zu\ncompiler=%s\nthread=%s\nrounds=%d\n"
                "warmup=1 untimed join per setting\n%sseconds_total=%.1f\n",
            SPEC_VERSION, date, cpu, u.sysname, u.release, u.machine, sysconf(_SC_NPROCESSORS_ONLN), ram_bytes(),
#ifdef __VERSION__
            __VERSION__,
#else
            "unknown",
#endif
            prio, rounds, seconds, total);
    fclose(mf);
    printf("\nTotal time: %.0f s. Files: %s/\n", total, dir);
    return 0;
}
