# Experiment 1: MonetDB's own join, fed the tuner's data.
# Needs MonetDB built into ../install first (see ../setup.sh).
# Same compiler flags as the tuner (../../tuner/Makefile), plus MonetDB's
# headers and kernel library. -DNDEBUG matches MonetDB's own release build
# (cmake -DASSERT=OFF); without it, gdk.h wraps malloc and free in logging code.
CC ?= cc
MONETDB ?= $(abspath ../install)
CFLAGS ?= -O3 -std=gnu11 -Wall -Wextra -Wpedantic
PC = PKG_CONFIG_PATH=$(MONETDB)/lib/pkgconfig pkg-config
MDB_CFLAGS = -DNDEBUG $(shell $(PC) --cflags monetdb-gdk libxxhash openssl)
MDB_LIBS = $(shell $(PC) --libs monetdb-gdk) -Wl,-rpath,$(MONETDB)/lib -lm
TUNER = ../../tuner/src/tune.c

all: bin/step1 bin/step2 bin/step3
step1: bin/step1
step2: bin/step2
step3: bin/step3

bin/step1: src/step1_hello.c src/monetdb_glue.h $(TUNER)
	@mkdir -p bin
	$(CC) $(CFLAGS) $(MDB_CFLAGS) -o $@ src/step1_hello.c $(MDB_LIBS)

bin/step2: src/step2_one_size.c src/monetdb_glue.h $(TUNER)
	@mkdir -p bin
	$(CC) $(CFLAGS) $(MDB_CFLAGS) -o $@ src/step2_one_size.c $(MDB_LIBS)

bin/step3: src/step3_build_vs_probe.c src/monetdb_glue.h $(TUNER)
	@mkdir -p bin
	$(CC) $(CFLAGS) $(MDB_CFLAGS) -o $@ src/step3_build_vs_probe.c $(MDB_LIBS)

clean:
	rm -rf bin

.PHONY: all step1 step2 step3 clean
