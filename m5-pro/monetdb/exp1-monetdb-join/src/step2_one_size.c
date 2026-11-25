/* Experiment 1, step 2: MonetDB's join on the tuner's data at one size.
 *
 * 1. Makes the tuner's tables (default 1,048,576 rows each).
 * 2. Runs one untimed join with MonetDB's join messages switched on, so it
 *    says which method it picked and which side got the hash table.
 * 3. Throws those columns away and makes new ones, so nothing MonetDB learned
 *    (a hash table, "this column is unique") carries over.
 * 4. Times one join, then times turning MonetDB's answer into the tuner's
 *    checksum, separately. Checks the answer.
 * 5. Times the tuner's own join once at the given setting, for scale.
 *
 * One timing each: this step checks that everything works at a real size.
 * It is not a result; step 3 repeats and shuffles.
 *
 *   ./bin/step2 [ROWS [BITS PASSES ROWS_PER_BUCKET]]
 *   defaults: 1048576 rows; 11 bits, 1 pass, 0.125 (the tuner's pick at 1M)
 */
#include "monetdb_config.h"
#include "gdk.h"

#define main tuner_main
#include "../../../tuner/src/tune.c"
#undef main

#include "monetdb_glue.h"

int main(int argc, char **argv) {
    size_t n = argc > 1 ? (size_t)strtoull(argv[1], NULL, 10) : (size_t)1 << 20;
    Knobs k = {11, 1, 0.125};
    if (argc > 4) k = (Knobs){(unsigned)atoi(argv[2]), (unsigned)atoi(argv[3]), atof(argv[4])};
    if (n < 2 || n > ((size_t)1 << 28)) die("ROWS must be 2 .. 268435456");
    if (k.bits > max_bits_for(n) || (k.bits == 0) != (k.passes == 0) || k.passes > MAX_PASSES || k.load <= 0)
        die("bad setting");
    char buf[128], cpu[128];
    cpu_name(cpu, sizeof cpu);
    printf("cpu: %s\npriority: %s\nrows per table: %zu\n", cpu, set_priority(buf, sizeof buf), n);
    if (start_monetdb() != 0) return EXIT_FAILURE;

    Work w = work_new(n); /* the tuner's tables and buffers, every page touched */
    generate(w.r, w.s, n, SEED);
    w.expected = expected_result(n);

    /* 2. Untimed join, with MonetDB's join messages on. */
    BAT *rk = key_column(w.r, n), *sk = key_column(w.s, n);
    BAT *out_s = NULL, *out_r = NULL;
    if (rk == NULL || sk == NULL) return EXIT_FAILURE;
    printf("\n--- MonetDB's join messages (untimed join; S is the left input, R the right) ---\n");
    fflush(stdout);
    if (GDKtracer_set_component_level("algo", "debug") != GDK_SUCCEED) die("cannot switch on join messages");
    if (BATjoin(&out_s, &out_r, sk, rk, NULL, NULL, false, BUN_NONE) != GDK_SUCCEED) die("BATjoin failed");
    GDKtracer_flush_buffer();
    GDKtracer_reset_component_level("algo");
    fflush(stdout);
    printf("--- end of messages ---\n");
    Result got = checksum_of(out_s, out_r, w.r, w.s);
    if (got.matches != w.expected.matches || got.checksum != w.expected.checksum) die("untimed join: wrong answer");
    BBPreclaim(out_s);
    BBPreclaim(out_r);
    BBPreclaim(rk);
    BBPreclaim(sk);

    /* 3 and 4. New columns, one timed join, then the checksum, timed apart. */
    rk = key_column(w.r, n);
    sk = key_column(w.s, n);
    if (rk == NULL || sk == NULL) return EXIT_FAILURE;
    uint64_t t0 = now_ns();
    gdk_return ok = BATjoin(&out_s, &out_r, sk, rk, NULL, NULL, false, BUN_NONE);
    uint64_t t1 = now_ns();
    if (ok != GDK_SUCCEED) die("BATjoin failed");
    got = checksum_of(out_s, out_r, w.r, w.s);
    uint64_t t2 = now_ns();
    if (got.matches != w.expected.matches || got.checksum != w.expected.checksum) die("timed join: wrong answer");
    double join_ns = (double)(t1 - t0) / (double)n, sum_ns = (double)(t2 - t1) / (double)n;
    printf("\nMonetDB BATjoin:              %7.2f ns per row  (%.1f ms)\n", join_ns, (t1 - t0) / 1e6);
    printf("checksum of its answer:       %7.2f ns per row  (%.1f ms)\n", sum_ns, (t2 - t1) / 1e6);
    printf("MonetDB total:                %7.2f ns per row\n", join_ns + sum_ns);
    printf("answer: %llu matches, checksum %016llx (correct)\n", (unsigned long long)got.matches,
           (unsigned long long)got.checksum);
    BBPreclaim(out_s);
    BBPreclaim(out_r);
    BBPreclaim(rk);
    BBPreclaim(sk);

    /* 5. The tuner's own join, once (measure() stops on a wrong answer). */
    double tuner_ns = measure(&w, k);
    printf("tuner's join (%s): %7.2f ns per row  (one run, answer checked)\n", knob_text(k, buf, sizeof buf),
           tuner_ns);
    work_free(&w);
    return EXIT_SUCCESS;
}
