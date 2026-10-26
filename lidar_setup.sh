#!/usr/bin/env bash
# Deprecated alias for one release: ./hw.sh lidar verify|snapshot|apply (also status, plan, rollback).
echo "lidar_setup.sh is deprecated; use: ./hw.sh lidar $*" >&2
exec "$(dirname -- "${BASH_SOURCE[0]}")/hw.sh" lidar "$@"
