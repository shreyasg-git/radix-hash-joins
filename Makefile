# Builds the benchmarks into bin/.
#   make            optimized builds
#   make test       correctness tests
#   make check      the tests under AddressSanitizer + UndefinedBehaviorSanitizer
#
# On macOS (Apple silicon) only the radix partitioning benchmark is built: the
# stride scan uses x86 cache-flush instructions and the simple hash join uses
# Linux CPU pinning.
CC      ?= gcc
OS      := $(shell uname -s)
ARCH    := $(shell uname -m)
CFLAGS  ?= -O2 -std=c11 -Wall -Wextra $(if $(filter Linux,$(OS)),-Werror,)
INCLUDE  = -Iinclude
SAN      = -O1 -g -std=c11 -Wall -Wextra -fsanitize=address,undefined -fno-omit-frame-pointer

ifeq ($(OS),Linux)
LINUX_ONLY = hash_join $(if $(filter x86_64 i686,$(ARCH)),scan_benchmark,)
endif

all: bin/radix_partition $(LINUX_ONLY:%=bin/%)

bin:
	mkdir -p bin

bin/scan_benchmark: src/scan_benchmark.c | bin
	$(CC) -O2 -std=c11 -Wall -Wextra -msse2 $< -o $@

bin/hash_join: src/hash_join.c | bin
	$(CC) $(CFLAGS) $< -o $@

bin/radix_partition: src/radix_partition.c include/common.h | bin
	$(CC) $(CFLAGS) $(INCLUDE) $< -o $@

test: all
	$(if $(filter hash_join,$(LINUX_ONLY)),./bin/hash_join --test,)
	./bin/radix_partition --test

check: | bin
	$(if $(filter hash_join,$(LINUX_ONLY)),$(CC) $(SAN) src/hash_join.c -o bin/hash_join_check && ./bin/hash_join_check --test,)
	$(CC) $(SAN) $(INCLUDE) src/radix_partition.c -o bin/radix_partition_check
	./bin/radix_partition_check --test

clean:
	rm -rf bin

.PHONY: all test check clean
