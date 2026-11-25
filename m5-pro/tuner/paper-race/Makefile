# Paper's formula against the tuner. Same compiler flags as the tuner (../Makefile).
CC ?= cc
CFLAGS ?= -O3 -std=gnu11 -Wall -Wextra -Wpedantic

bin/race: src/race.c ../src/tune.c
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ src/race.c -lm

clean:
	rm -rf bin

.PHONY: clean
