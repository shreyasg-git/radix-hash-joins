# M5 Pro (macOS, Apple silicon) builds of every experiment.
#   make         optimized builds into bin/
#   make test    correctness tests of every program
#   make check   the tests under AddressSanitizer + UndefinedBehaviorSanitizer
CC      ?= cc
CFLAGS  ?= -O2 -std=c11 -Wall -Wextra
INCLUDE  = -Iinclude
SAN      = -O1 -g -std=c11 -Wall -Wextra -fsanitize=address,undefined -fno-omit-frame-pointer
PROGRAMS = scan_benchmark hash_join hash_join_quarter radix_partition radix_join parallel_join bucket_sweep

all: $(PROGRAMS:%=bin/%)

bin:
	mkdir -p bin

bin/scan_benchmark: src/scan_benchmark.c | bin
	$(CC) $(CFLAGS) $< -o $@

# Week 2 bucket rule (at least 2 buckets per left row), as in reports/simple-hash-join.pdf
bin/hash_join: src/hash_join.c | bin
	$(CC) $(CFLAGS) $< -o $@

# Bucket rule from commit d28429e (about 1 bucket per 4 left rows)
bin/hash_join_quarter: src/hash_join.c | bin
	$(CC) $(CFLAGS) -DQUARTER_BUCKETS $< -o $@

bin/radix_partition: src/radix_partition.c include/common.h | bin
	$(CC) $(CFLAGS) $(INCLUDE) $< -o $@

bin/radix_join: src/radix_join.c include/common.h | bin
	$(CC) $(CFLAGS) $(INCLUDE) $< -o $@

bin/parallel_join: src/parallel_join.c include/common.h | bin
	$(CC) $(CFLAGS) $(INCLUDE) $< -o $@ -lpthread

# Experiment 6: bucket count and hash function
bin/bucket_sweep: src/bucket_sweep.c include/common.h | bin
	$(CC) $(CFLAGS) $(INCLUDE) $< -o $@ -lm

test: all
	./bin/hash_join --test
	./bin/hash_join_quarter --test
	./bin/radix_partition --test
	./bin/radix_join --test
	./bin/parallel_join --test
	./bin/bucket_sweep --test

check: | bin
	$(CC) $(SAN) src/hash_join.c -o bin/hash_join_check && ./bin/hash_join_check --test
	$(CC) $(SAN) $(INCLUDE) src/radix_partition.c -o bin/radix_partition_check && ./bin/radix_partition_check --test
	$(CC) $(SAN) $(INCLUDE) src/radix_join.c -o bin/radix_join_check && ./bin/radix_join_check --test
	$(CC) $(SAN) $(INCLUDE) src/bucket_sweep.c -o bin/bucket_sweep_check -lm && ./bin/bucket_sweep_check --test
	$(CC) -O1 -g -std=c11 -Wall -Wextra -fsanitize=thread $(INCLUDE) src/parallel_join.c -o bin/parallel_join_check -lpthread && ./bin/parallel_join_check --test

clean:
	rm -rf bin

.PHONY: all test check clean
