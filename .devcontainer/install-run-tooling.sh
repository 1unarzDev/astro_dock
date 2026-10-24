#!/usr/bin/env bash
# Local package installation only; no ROS imports, source-path imports or network.
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 -m venv --system-site-packages "$script_dir/run-tooling"
"$script_dir/run-tooling/bin/python" -c 'import yaml, setuptools' || {
    echo 'Install host python3-venv, python3-yaml and python3-setuptools first.' >&2; exit 2;
}
PIP_NO_INDEX=1 "$script_dir/run-tooling/bin/pip" install --no-deps --no-build-isolation \
    "$script_dir/../src/mhseals_config" "$script_dir/../tools" >&2
