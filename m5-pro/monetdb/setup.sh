#!/usr/bin/env bash
# Downloads MonetDB's code at the exact commit these experiments used and
# builds it into ./install. Nothing is
# installed outside this folder except the Homebrew tools listed below.
#   ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

COMMIT=f17907b90959fbde85465eafd5f28ae41ea393a0   # MonetDB master, 2026-11-25

# Build tools: cmake, bison 3 or newer (macOS ships 2.3), pkgconf, xxhash.
for formula in cmake bison pkgconf xxhash; do
  brew list --versions "$formula" >/dev/null || brew install "$formula"
done
export PATH="$(brew --prefix bison)/bin:$PATH"

if [[ ! -d source ]]; then
  # Full history, file contents fetched on demand: experiment 2 reads the
  # 2012 radix module from that history.
  git clone --filter=blob:none https://github.com/MonetDB/MonetDB.git source
fi
git -C source checkout --quiet "$COMMIT"

cmake -S source -B build -DCMAKE_BUILD_TYPE=Release -DASSERT=OFF -DSTRICT=OFF \
  -DGEOM=OFF -DODBC=OFF -DFITS=OFF -DNETCDF=OFF -DCMAKE_INSTALL_PREFIX="$PWD/install"
cmake --build build -j "$(sysctl -n hw.ncpu)"
cmake --build build --target install
echo "MonetDB is in $PWD/install"
