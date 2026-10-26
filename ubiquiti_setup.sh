#!/usr/bin/env bash
# Deprecated alias for one release: ./hw.sh radio VERB ... (same verbs; plan = apply --dry-run).
echo "ubiquiti_setup.sh is deprecated; use: ./hw.sh radio $*" >&2
exec "$(dirname -- "${BASH_SOURCE[0]}")/hw.sh" radio "$@"
