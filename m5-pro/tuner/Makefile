# Radix join tuner. Standalone: nothing here uses files outside tuner/.
CC ?= cc
CFLAGS ?= -O3 -std=gnu11 -Wall -Wextra -Wpedantic

bin/tune: src/tune.c
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ $< -lm

test: bin/tune
	./bin/tune --test

# Same tests with the address and undefined-behaviour checkers switched on.
check: test
	@mkdir -p bin
	$(CC) -O1 -g -std=gnu11 -fsanitize=address,undefined -fno-omit-frame-pointer -o bin/tune-asan src/tune.c -lm
	./bin/tune-asan --test

clean:
	rm -rf bin

.PHONY: test check clean
