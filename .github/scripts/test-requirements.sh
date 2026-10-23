#!/usr/bin/env bash
# The dev image must install a pyubx2 with PARSE_META, which the RTCM forwarding needs.
set -euo pipefail

req="$(cd "$(dirname "$0")/../.." && pwd)/.devcontainer/requirements.txt"
min=$(sed -n 's/^pyubx2>=\([0-9.]*\).*/\1/p' "$req")
[ -n "$min" ] || { echo "FAIL: pyubx2 has no minimum version in requirements.txt"; exit 1; }
[ "$(printf '%s\n1.3.9\n' "$min" | sort -V | head -1)" = "1.3.9" ] \
    || { echo "FAIL: pyubx2>=$min allows a version without PARSE_META (need >=1.3.9)"; exit 1; }
echo "ok"
