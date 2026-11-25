# Experiment 2: Boncz's radix join from MonetDB (2007), copied, on the tuner's data.
# Plain C: needs neither MonetDB's build nor its library. Same flags as the tuner.
CC ?= cc
CFLAGS ?= -O3 -std=gnu11 -Wall -Wextra -Wpedantic
TUNER = ../../tuner/src/tune.c

all: bin/step1 bin/step2
step1: bin/step1
step2: bin/step2

bin/step1: src/step1_check.c src/boncz_radix.h $(TUNER)
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ src/step1_check.c -lm

bin/step2: src/step2_one_size.c src/boncz_radix.h $(TUNER)
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ src/step2_one_size.c -lm

clean:
	rm -rf bin

.PHONY: all step1 step2 clean
