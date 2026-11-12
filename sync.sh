#!/bin/sh
# Copy the pipeline from the tcc spike into this repo. Run from anywhere; re-run after the spike changes.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/../cache"
for f in build.py package.py mirror_images.py; do cp "$SRC/$f" "$HERE/pipeline/$f"; done
echo "synced: $(ls "$HERE/pipeline" | tr '\n' ' ')"
