/* Experiment 2, step 2: Boncz's join and the tuner's join at the same bits
 * and passes, at one size.
 *
 * The tuner's join also gets its rows-per-box knob (default 1/8, its pick at
 * 1M rows); Boncz's join uses its own fixed rule (0.5 to 1 row per box). One
 * untimed warm-up each, then REPEATS repeats, the two alternating (Boncz first
 * in odd repeats). Every answer is checked. Also prints how Boncz's cuts
 * finished in one join.
 *
 *   ./bin/step2 [ROWS [BITS PASSES TUNER_ROWS_PER_BOX [REPEATS]]]
 *   defaults: 1048576 rows, 11 bits, 1 pass, 0.125, 5 repeats
 */
#define main tuner_main
#include "../../../tuner/src/tune.c"
#undef main

#include "boncz_radix.h"

static double boncz_measure(Work *w, unsigned bits, unsigned passes) {
    const uint64_t t0 = now_ns();
    const Result got = bz_run_join(w, bits, passes, 0);
    const uint64_t t1 = now_ns();
    if (got.matches != w->expected.matches || got.checksum != w->expected.checksum) die("Boncz's join: wrong answer");
    return (double)(t1 - t0) / (double)w->n;
}

int main(int argc, char **argv) {
    size_t n = argc > 1 ? (size_t)strtoull(argv[1], NULL, 10) : (size_t)1 << 20;
    Knobs k = {11, 1, 0.125};
    if (argc > 4) k = (Knobs){(unsigned)atoi(argv[2]), (unsigned)atoi(argv[3]), atof(argv[4])};
    int repeats = argc > 5 ? atoi(argv[5]) : 5;
    if (n < 2 || n > ((size_t)1 << 28) || repeats < 1 || repeats > 100) die("bad arguments");
    if (k.bits > max_bits_for(n) || (k.bits == 0) != (k.passes == 0) || k.passes > MAX_PASSES || k.load <= 0)
        die("bad setting");
    char buf[128], cpu[128], kt[96];
    cpu_name(cpu, sizeof cpu);
    printf("cpu: %s\npriority: %s\nrows per table: %zu, repeats: %d\n", cpu, set_priority(buf, sizeof buf), n,
           repeats);
    printf("tuner's join: %s\nBoncz's join: %u bits, %u pass%s, its own rows-per-box rule\n\n",
           knob_text(k, kt, sizeof kt), k.bits, k.passes, k.passes == 1 ? "" : "es");

    Work w = work_new(n);
    generate(w.r, w.s, n, SEED);
    w.expected = expected_result(n);

    bz_cuts = bz_fit_first = bz_sampled = bz_counted = 0;
    boncz_measure(&w, k.bits, k.passes); /* warm-up, and the cut counts of one join */
    printf("Boncz's cuts in one join (R and S): %zu; fitted the equal first shares: %zu; estimated: %zu; counted: %zu\n\n",
           bz_cuts, bz_fit_first, bz_sampled, bz_counted);
    measure(&w, k); /* warm-up */

    double sb = 0, st = 0;
    printf("repeat | Boncz's join | tuner's join   (ns per row)\n");
    for (int i = 1; i <= repeats; ++i) {
        double b, t;
        if (i % 2) { b = boncz_measure(&w, k.bits, k.passes); t = measure(&w, k); }
        else { t = measure(&w, k); b = boncz_measure(&w, k.bits, k.passes); }
        printf("%6d | %12.2f | %12.2f\n", i, b, t);
        sb += b;
        st += t;
    }
    printf("average| %12.2f | %12.2f   Boncz / tuner = %.2f\n", sb / repeats, st / repeats, sb / st);
    printf("every answer: %llu matches, checksum %016llx (correct)\n", (unsigned long long)w.expected.matches,
           (unsigned long long)w.expected.checksum);
    work_free(&w);
    return EXIT_SUCCESS;
}
