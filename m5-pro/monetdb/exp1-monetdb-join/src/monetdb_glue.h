/* The few lines that connect the tuner's data to MonetDB's kernel.
 * Included after gdk.h and the tuner's tune.c. */
#ifndef MONETDB_GLUE_H
#define MONETDB_GLUE_H

/* MonetDB's kernel, in memory only: no database folder, nothing on disk. */
static int start_monetdb(void) {
    if (BBPaddfarm(NULL, (1U << PERSISTENT) | (1U << TRANSIENT), false) != GDK_SUCCEED) {
        fprintf(stderr, "BBPaddfarm failed\n");
        return -1;
    }
    if (GDKinit(NULL, 0, true, NULL) != GDK_SUCCEED) {
        fprintf(stderr, "GDKinit failed\n");
        return -1;
    }
    return 0;
}

/* A MonetDB column holding the keys of `rec`, in the same order. Position i
 * of the column is row i of the tuner's table, so the tuner's row numbers can
 * be looked up from a matching position. The column's flags say "not known
 * to be sorted, not known to be unique": what MonetDB would know about a plain
 * column it has never looked at. */
static BAT *key_column(const Rec *rec, size_t n) {
    if (n > (size_t)INT_MAX) return NULL;
    BAT *b = COLnew(0, TYPE_int, (BUN)n, TRANSIENT);
    if (b == NULL) {
        fprintf(stderr, "COLnew failed\n");
        return NULL;
    }
    int *v = Tloc(b, 0);
    for (size_t i = 0; i < n; ++i) v[i] = (int)rec[i].key;
    BATsetcount(b, (BUN)n);
    if (n > 1) {
        b->tsorted = false;
        b->trevsorted = false;
        b->tkey = false;
    }
    b->tnosorted = b->tnorevsorted = 0;
    b->tnokey[0] = b->tnokey[1] = 0;
    b->tnonil = true; /* true: the keys are never MonetDB's "missing" value */
    b->tnil = false;
    b->tminpos = b->tmaxpos = BUN_NONE;
    b->tunique_est = 0;
    b->tseqbase = oid_nil;
    return b;
}

/* The tuner's answer (number of matches and checksum) from MonetDB's two
 * result columns: matching positions in S and in R. */
static Result checksum_of(BAT *out_s, BAT *out_r, const Rec *r, const Rec *s) {
    Result res = {0, 0};
    BATiter is = bat_iterator(out_s), ir = bat_iterator(out_r);
    for (BUN i = 0; i < is.count; ++i) {
        oid ps = BUNtoid(&is, i), pr = BUNtoid(&ir, i);
        ++res.matches;
        res.checksum += pair_hash(r[pr].row, s[ps].row);
    }
    bat_iterator_end(&is);
    bat_iterator_end(&ir);
    return res;
}

#endif
