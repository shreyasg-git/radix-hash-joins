/* Experiment 1, step 3: where MonetDB's join time goes.
 *
 * MonetDB's hash join does two jobs: build a hash table on R, then look up
 * every row of S in it. This step times them apart, on new columns every time:
 *   whole:  BATjoin alone (it builds the table itself, as in step 2);
 *   split:  BAThash(R) first (the same table BATjoin would build), then
 *           BATjoin, which now finds the table and only looks up.
 * The two kinds alternate (whole first in odd repeats, split first in even
 * ones), and every answer is checked. The name of the method MonetDB ran is
 * printed after every join.
 *
 *   ./bin/step3 [ROWS [REPEATS]]      defaults: 1048576 rows, 5 repeats
 */
#include "monetdb_config.h"
#include "gdk.h"

#define main tuner_main
#include "../../../tuner/src/tune.c"
#undef main

#include "monetdb_glue.h"

static double per_row(uint64_t t0, uint64_t t1, size_t n) { return (double)(t1 - t0) / (double)n; }

/* Joins new columns. Returns BATjoin's time per row; *build_ns gets BAThash's
 * time per row when `split`, else 0. Stops on a wrong answer. */
static double one_join(Work *w, int split, double *build_ns, char *algo, size_t algo_len) {
    BAT *rk = key_column(w->r, w->n), *sk = key_column(w->s, w->n), *out_s = NULL, *out_r = NULL;
    if (rk == NULL || sk == NULL) die("cannot make columns");
    *build_ns = 0;
    if (split) {
        uint64_t b0 = now_ns();
        if (BAThash(rk) != GDK_SUCCEED) die("BAThash failed");
        uint64_t b1 = now_ns();
        *build_ns = per_row(b0, b1, w->n);
    }
    MT_thread_setalgorithm(NULL, NULL); /* clear MonetDB's list of methods run by this thread */
    uint64_t t0 = now_ns();
    if (BATjoin(&out_s, &out_r, sk, rk, NULL, NULL, false, BUN_NONE) != GDK_SUCCEED) die("BATjoin failed");
    uint64_t t1 = now_ns();
    snprintf(algo, algo_len, "%s", MT_thread_getalgorithm() ? MT_thread_getalgorithm() : "?");
    Result got = checksum_of(out_s, out_r, w->r, w->s);
    if (got.matches != w->expected.matches || got.checksum != w->expected.checksum) die("wrong answer");
    BBPreclaim(out_s);
    BBPreclaim(out_r);
    BBPreclaim(rk);
    BBPreclaim(sk);
    return per_row(t0, t1, w->n);
}

int main(int argc, char **argv) {
    size_t n = argc > 1 ? (size_t)strtoull(argv[1], NULL, 10) : (size_t)1 << 20;
    int repeats = argc > 2 ? atoi(argv[2]) : 5;
    if (n < 2 || n > ((size_t)1 << 28) || repeats < 1 || repeats > 100) die("usage: step3 [ROWS [REPEATS]]");
    char buf[128], cpu[128], algo_whole[96], algo_split[96];
    cpu_name(cpu, sizeof cpu);
    printf("cpu: %s\npriority: %s\nrows per table: %zu, repeats: %d\n\n", cpu, set_priority(buf, sizeof buf), n,
           repeats);
    if (start_monetdb() != 0) return EXIT_FAILURE;
    Work w = work_new(n);
    generate(w.r, w.s, n, SEED);
    w.expected = expected_result(n);

    double sum_whole = 0, sum_build = 0, sum_probe = 0, unused;
    printf("repeat | whole BATjoin | split: build (BAThash) + look up (BATjoin) = total\n");
    for (int i = 1; i <= repeats; ++i) {
        double whole, build, probe;
        if (i % 2) {
            whole = one_join(&w, 0, &unused, algo_whole, sizeof algo_whole);
            probe = one_join(&w, 1, &build, algo_split, sizeof algo_split);
        } else {
            probe = one_join(&w, 1, &build, algo_split, sizeof algo_split);
            whole = one_join(&w, 0, &unused, algo_whole, sizeof algo_whole);
        }
        printf("%6d | %8.2f ns     | %8.2f + %8.2f = %8.2f ns per row\n", i, whole, build, probe, build + probe);
        sum_whole += whole;
        sum_build += build;
        sum_probe += probe;
    }
    printf("average| %8.2f ns     | %8.2f + %8.2f = %8.2f ns per row\n\n", sum_whole / repeats,
           sum_build / repeats, sum_probe / repeats, (sum_build + sum_probe) / repeats);
    printf("method MonetDB reported, whole: \"%s\"\n", algo_whole);
    printf("method MonetDB reported, split: \"%s\"\n", algo_split);
    printf("every answer: %llu matches, checksum %016llx (correct)\n", (unsigned long long)w.expected.matches,
           (unsigned long long)w.expected.checksum);
    work_free(&w);
    return EXIT_SUCCESS;
}
