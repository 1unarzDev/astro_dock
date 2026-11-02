#!/usr/bin/env bash
# The landing gate (under 60 s, nothing contacted, no containers): shell syntax and the fast unit tests of
# hw.sh, its drivers, setup's repos stage and the field commands. The full suite runs in CI and never blocks.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
start=$SECONDS
for script in setup.linux.sh hw.sh devices/*.sh devices/*/*.sh .devcontainer/*.sh tests/*.sh; do
    bash -n "$script"
done
python3 -m unittest -q tests.test_field tests.test_hw_contract tests.test_hosts tests.test_profiles \
    tests.test_setup_repos tests.test_readme tests.test_agnostic tests.test_install_check
echo "smoke OK in $((SECONDS - start)) s"
