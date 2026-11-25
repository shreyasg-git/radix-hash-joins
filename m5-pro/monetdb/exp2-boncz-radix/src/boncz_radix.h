/* Peter Boncz's radix cluster and partitioned hash join, as MonetDB had them.
 *
 * Source: MonetDB5/src/modules/mal/radix.mx at MonetDB commit 33a91903a7
 * (2007-09-12), the last version MonetDB compiled: on 2007-10-04 the build
 * file commented it out, and the file was deleted on 2012-03-28. The core
 * functions below are the same in the 2011 copy except for variable names.
 * `../fetch-original.sh` saves that file as `original/radix-2007.mx`; the
 * line numbers below point into it.
 *
 * Kept exactly: the key mixer (int_HUSSLE), the piece number (low bits of the
 * mixed key), the pass order (last argument's bits first, highest bits
 * first), the cutting method (start with equal space per piece, move rows
 * until a piece is full, then estimate the rest from what was moved or count
 * it, shift the pieces, finish), the piece walk of phash_join (find runs of
 * equal piece number in both inputs, no list of piece bounds), the one hash
 * table shared by all pieces and cleared per piece, its size rule (boxes: the
 * smallest power of two >= R's average piece size / hitrate; links: 4 x the
 * average piece, more if a piece is bigger), the box choice (the mixed key's
 * bits just above the piece bits), and 8-byte box and link entries (hash_t =
 * oid = 8 bytes on a 64-bit build, the 2007 default).
 *
 * Changed, and why:
 * 1. Rows are the tuner's 8-byte rows (Rec: key, row) and positions count
 *    rows, not bytes. The code path copied is Boncz's own for this shape:
 *    4-byte keys with 4-byte row numbers ("simple" hash_join, int heads).
 *    The two byte constants become rows: 20 * pieces * BUNsize bytes becomes
 *    20 * pieces rows; 32,768 bytes becomes 32,768 / 8 = 4,096 rows.
 * 2. The big arrays (each pass's output) are the tuner's buffers, set up
 *    before the clock starts; radix.mx made a new BAT for every pass. The
 *    small per-call arrays (counts, cursors, bounds, moves, hash table) are
 *    still made and freed per call, as in radix.mx.
 * 3. A match adds to the tuner's checksum instead of being written out, as
 *    the tuner's join and experiment 1 do.
 * 4. `cutoff` (stop at the first match when R's keys are known unique) is a
 *    parameter; radix.mx's default was key(r). The tuner never stops early,
 *    so the experiments pass 0.
 * 5. move_chunks copies with memmove instead of memcpy: the same bytes when
 *    the two areas do not overlap, and defined when they do.
 *
 * Needs tune.c (Rec, Result, pair_hash, die) included first.
 */
#ifndef BONCZ_RADIX_H
#define BONCZ_RADIX_H

#include <stddef.h>
#ifndef MAX
#define MAX(a, b) ((a) > (b) ? (a) : (b)) /* as in MonetDB's gdk.h */
#endif
#ifndef MIN
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#endif

typedef uint64_t bz_hash_t;            /* hash_t (gdk.mx:587): oid, 8 bytes */
#define BZ_NONE (~(bz_hash_t)0)
#define BZ_MAX_PASSES 8

/* How often each way of finishing a cut ran (for the tests, not timed). */
static size_t bz_cuts = 0, bz_fit_first = 0, bz_sampled = 0, bz_counted = 0;

/* radix.mx:1270 */
static inline uint32_t bz_hussle(uint32_t x) { return (x >> 7) ^ (x >> 13) ^ (x >> 21) ^ x; }
/* int_RADIX, radix.mx:1761 */
static inline size_t bz_radix(uint32_t key, size_t mask) { return (size_t)bz_hussle(key) & mask; }

typedef struct {
    size_t src;  /* offset of chunk where it is now */
    size_t dst;  /* offset of chunk where it should go */
    size_t size; /* size of chunk (in rows) */
} bz_move_t;

/* radix.mx:1795. Block sequences copied to the right go right to left. */
static void bz_move_chunks(bz_move_t *mov, Rec *base, size_t from, size_t end) {
    while (from < end) {
        ptrdiff_t cur, cop = (ptrdiff_t)from;
        while (++from < end)
            if (mov[from].src >= mov[from].dst) break;
        for (cur = (ptrdiff_t)from - 1; cur >= cop; cur--)
            if (mov[cur].size) memmove(base + mov[cur].dst, base + mov[cur].src, mov[cur].size * sizeof(Rec));
    }
}

/* radix.mx:1814, without the `limits` bookkeeping (radix_cluster's optional
 * output of piece bounds, which phash_join does not use). */
static void bz_rearrange_chunks(bz_move_t *mov, Rec *base, size_t *dst, size_t *lim, const size_t *cnt, size_t n) {
    size_t i, cur = 0, start = 0;
    for (i = 0; i < n; i++) {
        size_t end = dst[i];
        size_t siz = end - start;
        size_t nxt = cur + siz + cnt[i];
        if (start <= cur && end >= cur) { /* overlap at start of cur */
            mov[i].src = start;
            mov[i].dst = end;
            mov[i].size = cur - start;
        } else if (start >= cur && start <= nxt) { /* overlap just before nxt */
            size_t hole = start - cur;
            if (hole > siz) hole = siz;
            mov[i].src = end - hole;
            mov[i].dst = cur;
            mov[i].size = hole;
        } else { /* no overlap: copy all */
            mov[i].src = start;
            mov[i].dst = cur;
            mov[i].size = siz;
        }
        start = lim[i];
        lim[i] = cur = nxt;
        dst[i] = nxt - cnt[i];
    }
    bz_move_chunks(mov, base, 0, n);
}

/* radix.mx:1882. Extrapolate the rows moved so far as if they were a sample. */
static void bz_sample_buns(size_t src, size_t cur, size_t end, const size_t *dst, const size_t *lim, size_t *cnt,
                           size_t n) {
    size_t ntuples = end - src;
    size_t done = cur - src;
    size_t total = ntuples - done;
    size_t i, oldlim;
    for (oldlim = i = 0; i < n; oldlim = lim[i], i++) {
        cnt[i] = ((dst[i] - oldlim) * (ntuples - done)) / done;
        total -= cnt[i];
    }
    for (i = 0; total > 0; total--) { /* add left-overs */
        cnt[i] += 1;
        if (++i >= n) i = 0;
    }
}

/* radix_buns, radix.mx:1906 (the int-head case). Moves rows into their
 * pieces until a piece is full; returns where it stopped. */
static size_t bz_radix_buns(const Rec *in, size_t start, size_t end, Rec *base, size_t *cur, const size_t *lim,
                            size_t mask, int shift) {
    size_t src = start;
    while (src < end) {
        size_t x = bz_radix(in[src].key, mask) >> shift;
        if (cur[x] == lim[x]) break;
        base[cur[x]] = in[src];
        cur[x] += 1;
        src += 1;
    }
    return src;
}

/* cnt_buns, radix.mx:2019. Count what is left for each chunk. */
static void bz_cnt_buns(const Rec *in, size_t src, size_t end, size_t *cnt, size_t mask, int shift) {
    for (; src < end; src++) cnt[bz_radix(in[src].key, mask) >> shift] += 1;
}

/* radix_chunk, radix.mx:2053. One cut of in[start, end) into 2^nbits pieces
 * at base[0, end - start). lim[] gets each piece's end. */
static void bz_radix_chunk(const Rec *in, size_t start, size_t init, size_t end, Rec *base, size_t *lim, int nbits,
                           int shift, size_t *dst, size_t *cnt) {
    size_t ntuples = end - start;
    size_t nchunks = (size_t)1 << nbits;
    size_t chunksize = ntuples / nchunks;
    size_t i;
    size_t mask = (nchunks - 1) << shift;
    bz_move_t buf[512], *mov = (nchunks > 512) ? malloc(nchunks * sizeof(bz_move_t)) : buf;
    if (mov == NULL) die("out of memory");

    for (i = 0; i < nchunks; i++) { /* kick off with uniform boundaries */
        lim[i] = chunksize;
        ntuples -= chunksize;
    }
    for (i = 0; ntuples > 0; ntuples--) {
        lim[i] += 1;
        if (++i >= nchunks) i = 0;
    }
    for (dst[0] = 0, i = 1; i < nchunks; i++) {
        dst[i] = lim[i - 1];
        lim[i] += dst[i];
    }
    /* signed, as the pointer arithmetic of radix.mx was: end - 4096 may lie before start */
    const ptrdiff_t lo_limit = (ptrdiff_t)start + (ptrdiff_t)MAX((size_t)(0.1 * (double)(init - start)), 20 * nchunks);
    const ptrdiff_t hi_limit =
        MIN((ptrdiff_t)start + (ptrdiff_t)(0.9 * (double)lim[i - 1]), (ptrdiff_t)end - 32768 / (ptrdiff_t)sizeof(Rec));

    size_t cur = bz_radix_buns(in, start, init, base, dst, lim, mask, shift);
    ++bz_cuts;
    if (cur == end) ++bz_fit_first;

    /* out of memory in some bucket: sample and continue */
    if ((ptrdiff_t)cur < hi_limit && (ptrdiff_t)cur > lo_limit) {
        ++bz_sampled;
        bz_sample_buns(start, cur, end, dst, lim, cnt, nchunks);
        bz_rearrange_chunks(mov, base, dst, lim, cnt, nchunks);
        cur = bz_radix_buns(in, cur, end, base, dst, lim, mask, shift);
    }
    /* out of memory in some bucket: count and finish */
    if (cur < end) {
        ++bz_counted;
        memset(cnt, 0, nchunks * sizeof(size_t));
        bz_cnt_buns(in, cur, end, cnt, mask, shift);
        bz_rearrange_chunks(mov, base, dst, lim, cnt, nchunks);
        cur = bz_radix_buns(in, cur, end, base, dst, lim, mask, shift);
        if (cur != end) die("boncz radix_chunk: rows left after counting");
    }
    if (nchunks > 512) free(mov);
}

/* radix_cluster_va, radix.mx:2115, with perc = 1.0 and no limits BAT.
 * radix[0 .. argc-1] are the bits per pass as radix_cluster(b, radix[0], ...)
 * takes them: passes run from radix[argc-1] (the highest bits) down to
 * radix[0]. Output alternates between a and b; returns the last one (or `in`
 * when there are no passes). */
static const Rec *bz_radix_cluster(const Rec *in, size_t cap, const int *radix, int argc, Rec *a, Rec *b) {
    int shift = 0;
    for (int i = 0; i < argc; i++) shift += radix[i];
    size_t n = 1;
    size_t *lim = malloc(sizeof(size_t));
    if (lim == NULL) die("out of memory");
    lim[0] = cap; /* just start with one cluster: the entire bat */
    const Rec *prev = in;
    Rec *bn = a;
    while (--argc >= 0 && radix[argc] > 0) {
        size_t i, h = (size_t)1 << radix[argc], j = h, *k, *l = lim;
        size_t *dst = malloc(sizeof(size_t) * h), *cnt = malloc(sizeof(size_t) * h);
        if (dst == NULL || cnt == NULL) die("out of memory");
        if (argc == 0) h = 0; /* last radix: we can use the same lim for each i */
        else j *= n;          /* get n lims; next radix reuses them as l[] boundary */
        k = lim = malloc(j * sizeof(size_t));
        if (lim == NULL) die("out of memory");
        shift -= radix[argc];
        for (j = i = 0; i < n; j = l[i], i++) {
            size_t *kk = k + h;
            bz_radix_chunk(prev, j, l[i], l[i], bn + j, k, radix[argc], shift, dst, cnt);
            while (k < kk) *(k++) += j; /* make relative limits absolute ones */
        }
        n *= h;
        free(l);
        free(dst);
        free(cnt);
        prev = bn;
        bn = (bn == a) ? b : a;
    }
    free(lim);
    return prev;
}

typedef struct {
    bz_hash_t *hash, *link;
    size_t mask, lim;
} bz_hash;

/* hash_join, radix.mx:2547, with the "simple" int body of radix.mx:2627.
 * Builds on r[rlo, rhi), probes with l[llo, lhi). rd = the number of radix bits. */
static void bz_hash_join(bz_hash *h, const Rec *l, size_t llo, size_t lhi, const Rec *r, size_t rlo, size_t rhi,
                         int rd, int cutoff, Result *res) {
    const size_t zz = rlo;
    bz_hash_t yy = 0;
    if (rhi - rlo > h->lim) { /* simplistic skew handling by holding on to initial hash mask size */
        h->lim = rhi - rlo;
        h->hash = realloc(h->hash, (h->mask + 1 + h->lim) * sizeof(bz_hash_t));
        if (h->hash == NULL) die("out of memory");
        h->link = h->hash + h->mask + 1;
    }
    memset(h->hash, ~0, (h->mask + 1) * sizeof(bz_hash_t));
    const size_t mask = h->mask << rd;
    for (; rlo < rhi; rlo++) { /* build phase */
        bz_hash_t v = (bz_hash_t)(bz_radix(r[rlo].key, mask) >> rd);
        h->link[yy] = h->hash[v];
        h->hash[v] = yy++;
    }
    for (; llo < lhi; llo++) { /* probe phase */
        const uint32_t v = l[llo].key;
        for (yy = h->hash[bz_radix(v, mask) >> rd]; yy != BZ_NONE; yy = h->link[yy]) {
            const Rec *m = &r[yy + zz];
            if (m->key == v) {
                ++res->matches;
                res->checksum += pair_hash(m->row, l[llo].row);
                if (cutoff) break;
            }
        }
    }
}

/* phash_join, radix.mx:2650, with hash_merge (radix.mx:2791). l and r are
 * cut on the same `radix` bits; l is probed, r gets the hash tables. */
static void bz_phash_join(const Rec *l, size_t ln, const Rec *r, size_t rn, int radix, int hitrate, int cutoff,
                          Result *res) {
    size_t k, m, rd = ((size_t)1 << radix) - 1;
    bz_hash h;
    h.lim = rn >> radix; /* mean cluster size */
    k = h.lim / (size_t)hitrate;           /* mean number of different elements per cluster */
    for (m = 1; m < k; m <<= 1) continue; /* perfect hashing */
    h.lim <<= 2;                           /* make lim four times as big for handling some skew */
    h.hash = malloc((m + h.lim) * sizeof(bz_hash_t));
    if (h.hash == NULL) die("out of memory");
    h.link = h.hash + m;
    h.mask = m - 1;
    if (rn) {
        if (rd == 0) { /* with zero bits, it is fairer not to perform a radix merge */
            bz_hash_join(&h, l, 0, ln, r, 0, rn, radix, cutoff, res);
        } else {
            size_t l_cur = 0, l_end, r_cur = 0, r_end;
            size_t y = bz_radix(r[r_cur].key, rd);
            if (!h.mask) h.mask = 1; /* "hack alert" in radix.mx; never reached with >= 16 rows per piece */
            while (l_cur < ln) { /* merge join on phash number */
                size_t x = bz_radix(l[l_cur].key, rd); /* find l range */
                for (l_end = l_cur + 1; l_end < ln; l_end++)
                    if (bz_radix(l[l_end].key, rd) != x) break;
                while (y < x) { /* find matching r */
                    if (++r_cur >= rn) goto xit;
                    y = bz_radix(r[r_cur].key, rd);
                }
                if (x == y) { /* phash hits found: find r range */
                    for (r_end = r_cur + 1; r_end < rn; r_end++) {
                        y = bz_radix(r[r_end].key, rd);
                        if (y != x) break;
                    }
                    bz_hash_join(&h, l, l_cur, l_end, r, r_cur, r_end, radix, cutoff, res);
                    r_cur = r_end;
                }
                l_cur = l_end;
            }
        }
    }
xit:
    free(h.hash);
}

/* The whole join with the tuner's knobs: `bits` split over `passes` the way
 * the tuner splits them (earlier passes get the extra bit), given to
 * radix_cluster in its order (first argument = last pass). hitrate 1. */
static Result bz_run_join(Work *w, unsigned bits, unsigned passes, int cutoff) {
    Result res = {0, 0};
    if (bits == 0) {
        bz_phash_join(w->s, w->n, w->r, w->n, 0, 1, cutoff, &res);
        return res;
    }
    if (passes < 1 || passes > BZ_MAX_PASSES || passes > bits) die("bad passes");
    int radix[BZ_MAX_PASSES];
    for (unsigned p = 0; p < passes; ++p) radix[passes - 1 - p] = (int)(bits / passes + (p < bits % passes));
    const Rec *r = bz_radix_cluster(w->r, w->n, radix, (int)passes, w->ra, w->rb);
    const Rec *s = bz_radix_cluster(w->s, w->n, radix, (int)passes, w->sa, w->sb);
    bz_phash_join(s, w->n, r, w->n, (int)bits, 1, cutoff, &res);
    return res;
}

#endif
