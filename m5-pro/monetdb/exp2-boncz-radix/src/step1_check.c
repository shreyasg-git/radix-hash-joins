/* Experiment 2, step 1: does the copy of Boncz's radix join give right answers?
 *
 * For 12 table sizes (1 to 262,144 rows, including odd sizes, so pieces are
 * uneven), every bits setting the tuner allows (0 .. log2(rows) - 4, at most
 * 18), every number of passes (1 .. 3, at most the bits), and with and
 * without stopping at the first match: compares the number of matches and
 * the checksum with the tuner's expected answer, and at up to 4,097 rows also
 * with the tuner's nested-loop join. Nothing is timed.
 *
 * Also counts how each cut finished: everything fitted the equal first
 * shares, the rest was estimated from what had been moved, or the rest was
 * counted.
 *
 *   ./bin/step1
 */
#define main tuner_main
#include "../../../tuner/src/tune.c"
#undef main

#include "boncz_radix.h"

int main(void) {
    const size_t sizes[] = {1, 2, 3, 100, 1000, 4097, 12345, 65536, 65537, 100000, 131071, 262144};
    size_t checks = 0;
    for (size_t si = 0; si < sizeof sizes / sizeof sizes[0]; ++si) {
        const size_t n = sizes[si];
        Work w = work_new(n);
        generate(w.r, w.s, n, SEED);
        w.expected = expected_result(n);
        if (n <= 4097) {
            Result nl = nested_loop(w.r, n, w.s, n);
            if (nl.matches != w.expected.matches || nl.checksum != w.expected.checksum)
                die("nested loop and expected answer differ");
        }
        size_t before = checks;
        for (unsigned bits = 0; bits <= max_bits_for(n); ++bits) {
            for (unsigned passes = bits ? 1 : 0; passes <= (bits ? (bits < 3 ? bits : 3) : 0); ++passes) {
                for (int cutoff = 0; cutoff <= 1; ++cutoff) {
                    Result got = bz_run_join(&w, bits, passes, cutoff);
                    ++checks;
                    if (got.matches != w.expected.matches || got.checksum != w.expected.checksum) {
                        fprintf(stderr, "WRONG at %zu rows, %u bits, %u passes, cutoff %d: %llu matches (want %llu)\n",
                                n, bits, passes, cutoff, (unsigned long long)got.matches,
                                (unsigned long long)w.expected.matches);
                        return EXIT_FAILURE;
                    }
                }
            }
        }
        printf("%7zu rows: %3zu settings right (bits 0..%u)\n", n, checks - before, max_bits_for(n));
        work_free(&w);
    }
    printf("\nall %zu checks right\n", checks);
    printf("how the %zu cuts finished (a cut can both estimate and then count):\n", bz_cuts);
    printf("  everything fitted the equal first shares: %zu\n", bz_fit_first);
    printf("  rest estimated from the rows moved so far: %zu\n", bz_sampled);
    printf("  rest counted exactly: %zu\n", bz_counted);
    return EXIT_SUCCESS;
}
