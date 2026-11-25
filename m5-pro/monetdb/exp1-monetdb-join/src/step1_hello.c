/* Experiment 1, step 1: can our own program call MonetDB's join at all?
 *
 * Starts MonetDB's kernel (the part below SQL, called GDK) in memory, makes
 * the tuner's two tables at 10 rows each (same generator, same seed), joins
 * them with MonetDB's BATjoin, prints every matching pair, and checks the
 * count and checksum against the tuner's own nested-loop join.
 *
 * Build and run:  make step1 && ./bin/step1
 */
#include "monetdb_config.h"
#include "gdk.h"

/* The tuner's code, unchanged: its data generator, its checksum and its
 * nested-loop join. Its main() is renamed so this file can have its own. */
#define main tuner_main
#include "../../../tuner/src/tune.c"
#undef main

#include "monetdb_glue.h"

int main(void) {
    const size_t n = 10;
    if (start_monetdb() != 0) return EXIT_FAILURE;

    Rec *r = touched(n * sizeof(Rec)), *s = touched(n * sizeof(Rec));
    generate(r, s, n, SEED);

    printf("R (build side): key / row\n");
    for (size_t i = 0; i < n; ++i) printf("  position %zu: key %2u, row %u\n", i, r[i].key, r[i].row);
    printf("S (probe side): key / row\n");
    for (size_t i = 0; i < n; ++i) printf("  position %zu: key %2u, row %u\n", i, s[i].key, s[i].row);

    BAT *rk = key_column(r, n), *sk = key_column(s, n);
    if (rk == NULL || sk == NULL) return EXIT_FAILURE;

    BAT *out_s = NULL, *out_r = NULL; /* matching positions in S and in R */
    if (BATjoin(&out_s, &out_r, sk, rk, NULL, NULL, false, BUN_NONE) != GDK_SUCCEED) {
        fprintf(stderr, "BATjoin failed: %s\n", GDKerrbuf);
        return EXIT_FAILURE;
    }

    printf("MonetDB's matches (S position, R position, key):\n");
    BATiter is = bat_iterator(out_s), ir = bat_iterator(out_r);
    for (BUN i = 0; i < is.count; ++i) {
        oid ps = BUNtoid(&is, i), pr = BUNtoid(&ir, i);
        printf("  S %llu, R %llu, key %u\n", (unsigned long long)ps, (unsigned long long)pr, s[ps].key);
    }
    bat_iterator_end(&is);
    bat_iterator_end(&ir);

    Result got = checksum_of(out_s, out_r, r, s);
    Result want = nested_loop(r, n, s, n);
    printf("MonetDB:     %llu matches, checksum %016llx\n", (unsigned long long)got.matches,
           (unsigned long long)got.checksum);
    printf("Nested loop: %llu matches, checksum %016llx\n", (unsigned long long)want.matches,
           (unsigned long long)want.checksum);
    int ok = got.matches == want.matches && got.checksum == want.checksum;
    printf("%s\n", ok ? "SAME ANSWER" : "DIFFERENT ANSWER");

    BBPreclaim(out_s);
    BBPreclaim(out_r);
    BBPreclaim(rk);
    BBPreclaim(sk);
    free(r);
    free(s);
    return ok ? EXIT_SUCCESS : EXIT_FAILURE;
}
