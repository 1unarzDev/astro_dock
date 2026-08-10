#!/usr/bin/env bash
set -euo pipefail

# Some vendor GPU images install Python packages with a restrictive build-time
# umask. Python enumerates every distribution on sys.path, so one root-only
# metadata directory breaks unrelated non-root setup.py/colcon builds.
while IFS= read -r packages; do
    chmod -R a+rX "$packages"
done < <(
    find /usr/local/lib /opt/ros /workspace \
        -type d \( -name dist-packages -o -name site-packages \) \
        -print 2>/dev/null || true
)
