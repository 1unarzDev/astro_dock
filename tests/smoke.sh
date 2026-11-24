#!/usr/bin/env bash
# The landing gate (nothing contacted, no containers): shell syntax and the fast unit tests of
# hw.sh, its drivers, setup's repos stage, the field commands and the agent command hook. The full suite runs in CI and never blocks.
#   tests/smoke.sh
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ $# == 0 ]] || { echo 'Usage: tests/smoke.sh' >&2; exit 2; }
start=$SECONDS
for script in setup.linux.sh hw.sh devices/*.sh devices/*/*.sh .devcontainer/*.sh tests/*.sh; do
    bash -n "$script"
done
modules=(tests.test_host_deps tests.test_field tests.test_hw_contract tests.test_hosts tests.test_profiles tests.test_setup_repos
         tests.test_readme tests.test_agnostic tests.test_install_check tests.test_overlay tests.test_build
         tests.test_agents_hook tests.test_run_env)
python3 -m unittest -q "${modules[@]}"
echo "smoke OK in $((SECONDS - start)) s"
