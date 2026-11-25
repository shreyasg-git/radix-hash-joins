#!/usr/bin/env bash
# Saves Boncz's radix module as MonetDB last compiled it (commit 33a91903a7,
# 2007-09-12) to original/radix-2007.mx, so src/boncz_radix.h can be checked
# line by line. Needs ../source (run ../setup.sh first). original/ is not committed.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p original
git -C ../source show 33a91903a7:MonetDB5/src/modules/mal/radix.mx > original/radix-2007.mx
echo "saved original/radix-2007.mx ($(wc -l < original/radix-2007.mx) lines)"
