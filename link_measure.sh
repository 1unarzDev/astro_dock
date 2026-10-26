#!/usr/bin/env bash
# Deprecated alias for one release: ./hw.sh radio measure|propose. LINK_* map to the profile overrides.
echo "link_measure.sh is deprecated; use: ./hw.sh radio $*" >&2
[[ ! -v LINK_SECONDS ]] || export HW_MEASURE_SECONDS=$LINK_SECONDS
[[ ! -v LINK_PORT ]] || export HW_MEASURE_PORT=$LINK_PORT
[[ ! -v LINK_CONTAINER ]] || export HW_MEASURE_CONTAINER=$LINK_CONTAINER
[[ ! -v LINK_RANGE_M ]] || export HW_PROPOSE_RANGE_M=$LINK_RANGE_M
[[ ! -v LINK_FREQ ]] || export HW_PROPOSE_FREQ=$LINK_FREQ
[[ ! -v LINK_WIDTH_MAX ]] || export HW_PROPOSE_WIDTH_MAX=$LINK_WIDTH_MAX
[[ ! -v LINK_TXPOWER_MAX ]] || export HW_PROPOSE_TXPOWER_MAX=$LINK_TXPOWER_MAX
exec "$(dirname -- "${BASH_SOURCE[0]}")/hw.sh" radio "$@"
