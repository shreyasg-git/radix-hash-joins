/* Experiment 1, step 4: MonetDB's join against the tuner's pick, measured
 * like the race (../../../tuner/paper-race/src/race.c).
 *
 * plan.csv has one line per setting: rows_per_input,label,bits,passes,rows_per_bucket.
 * Label "monetdb" is MonetDB's whole BATjoin on new columns, plus turning its
 * answer into the tuner's checksum (the tuner's join makes the checksum inside
 * its own timing, so the two are timed alike); its bits/passes/rows_per_bucket
 * are ignored. Any other label is the tuner's join at that setting.
 *
 * Per size: every setting gets 1 untimed warm-up, then ROUNDS rounds, each
 * round running every setting once in a newly shuffled order. Every answer is
 * checked; a wrong one stops the run.
 *
 *   ./bin/step4 PLAN_CSV OUT_DIR ROUNDS
 */
#include "monetdb_config.h"
#include "gdk.h"

#define main tuner_main
#include "../../../tuner/src/tune.c"
#undef main

#include "monetdb_glue.h"

#define MAX_PLAN 64

typedef struct {
    size_t n;
    char label[32];
    Knobs k;
    int monetdb;
    double sum;
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
        x.monetdb = !strcmp(x.label, "monetdb");
        if (!x.monetdb && (x.k.bits > max_bits_for(x.n) || x.k.passes > x.k.bits || (x.k.bits && !x.k.passes) ||
                           x.k.load < LOADS[0] - 1e-12))
            die("a plan setting is outside what the tuner's join supports");
        e[count++] = x;
    }
    fclose(f);
    if (!count) die("the plan has no settings");
    return count;
}

/* One MonetDB join on new columns. Returns join + checksum time per row and
 * gives the two parts and the method MonetDB reported. */
static double monetdb_measure(Work *w, double *join_ns, double *check_ns, char *method, size_t len) {
    BAT *rk = key_column(w->r, w->n), *sk = key_column(w->s, w->n), *out_s = NULL, *out_r = NULL;
    if (rk == NULL || sk == NULL) die("cannot make MonetDB columns");
    MT_thread_setalgorithm(NULL, NULL);
    const uint64_t t0 = now_ns();
    if (BATjoin(&out_s, &out_r, sk, rk, NULL, NULL, false, BUN_NONE) != GDK_SUCCEED) die("BATjoin failed");
    const uint64_t t1 = now_ns();
    const Result got = checksum_of(out_s, out_r, w->r, w->s);
    const uint64_t t2 = now_ns();
    snprintf(method, len, "%s", MT_thread_getalgorithm() ? MT_thread_getalgorithm() : "?");
    if (got.matches != w->expected.matches || got.checksum != w->expected.checksum) {
        fprintf(stderr, "step4: MonetDB gave a wrong answer at %zu rows\n", w->n);
        exit(EXIT_FAILURE);
    }
    BBPreclaim(out_s);
    BBPreclaim(out_r);
    BBPreclaim(rk);
    BBPreclaim(sk);
    *join_ns = (double)(t1 - t0) / (double)w->n;
    *check_ns = (double)(t2 - t1) / (double)w->n;
    return *join_ns + *check_ns;
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

    char cpu[256], prio[128], a1[96], method[160], seconds[512] = "";
    cpu_name(cpu, sizeof cpu);
    set_priority(prio, sizeof prio);
    struct utsname u;
    uname(&u);
    if (start_monetdb() != 0) return EXIT_FAILURE;
    printf("MonetDB's join against the tuner's pick, on %s (%s %s)\n", cpu, u.sysname, u.release);
    printf("Thread: %s. Every setting: 1 untimed warm-up, then %d rounds in shuffled order.\n", prio, rounds);

    FILE *trials = open_csv(dir, "trials.csv",
                            "rows_per_input,label,bits,passes,rows_per_bucket,round,position,ns_per_row,"
                            "join_ns_per_row,checksum_ns_per_row,method");
    const uint64_t t_start = now_ns();
    uint64_t state = SEED;
    double jn, cn;
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
            if (plan[k].monetdb) monetdb_measure(&w, &jn, &cn, method, sizeof method);
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
                if (e->monetdb) {
                    const double ns = monetdb_measure(&w, &jn, &cn, method, sizeof method);
                    e->sum += ns;
                    fprintf(trials, "%zu,%s,,,,%d,%d,%.4f,%.4f,%.4f,%s\n", n, e->label, r + 1, pos + 1, ns, jn, cn,
                            method);
                } else {
                    const double ns = measure(&w, e->k);
                    e->sum += ns;
                    fprintf(trials, "%zu,%s,%u,%u,%g,%d,%d,%.4f,%.4f,,radix join (tuner spec 1)\n", n, e->label,
                            e->k.bits, e->k.passes, e->k.load, r + 1, pos + 1, ns, ns);
                }
            }
        }
        fflush(trials);
        work_free(&w);
        const double s = (double)(now_ns() - t0) / 1e9;
        for (int k = i; k < j; ++k)
            printf("  %-8s %-44s average %6.2f ns per row\n", plan[k].label,
                   plan[k].monetdb ? "MonetDB BATjoin + checksum" : knob_text(plan[k].k, a1, sizeof a1),
                   plan[k].sum / rounds);
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
    fprintf(mf, "tuner_spec=%d\nmonetdb_version=%s\ndate=%s\ncpu=%s\nos=%s %s %s\nlogical_cpus=%ld\nram_bytes=%zu\n"
                "compiler=%s\nthread=%s\nrounds=%d\nwarmup=1 untimed join per setting\n%sseconds_total=%.1f\n",
            SPEC_VERSION, GDKversion(false), date, cpu, u.sysname, u.release, u.machine, sysconf(_SC_NPROCESSORS_ONLN),
            ram_bytes(),
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
