#!/usr/bin/env bash
# Deprecated alias for one release: ./hw.sh jetson apply USER@HOST [BUILD_RUN_ID]
# (ASTRO_JETSON_GATEWAY maps to HW_GATEWAY, the jetson.yaml gateway override).
echo "promote_jetson.sh is deprecated; use: ./hw.sh jetson apply $*" >&2
[[ ! -v ASTRO_JETSON_GATEWAY ]] || export HW_GATEWAY=$ASTRO_JETSON_GATEWAY
exec "$(dirname -- "${BASH_SOURCE[0]}")/hw.sh" jetson apply "$@"
