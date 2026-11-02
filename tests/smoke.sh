#!/usr/bin/env bash
# The landing gate (under 60 s, nothing contacted, no containers): shell syntax and the fast unit tests of
# hw.sh, its drivers, setup's repos stage and the field commands. The full suite runs in CI and never blocks.
#   tests/smoke.sh [--impact]   --impact: syntax, then only the tests timpact selects (ALL: the full smoke)
# With the workspace agent tools (MHSEALS_AGENTS, else src/mhseals_agents of the main checkout), a tree that
# already passed is not run again (tcache, tier smoke or smoke-impact); ASTRO_SMOKE_UNCACHED=1 skips that.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
impact=false
case ${1:-} in
    --impact) impact=true ;;
    '') ;;
    *) echo 'Usage: tests/smoke.sh [--impact]' >&2; exit 2 ;;
esac
agents=${MHSEALS_AGENTS:-}
if [[ -z $agents ]] && common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null); then
    agents=$(dirname -- "$common")/src/mhseals_agents
fi
if [[ -z ${ASTRO_SMOKE_UNCACHED:-} && -x $agents/tools/tcache ]]; then
    export ASTRO_SMOKE_UNCACHED=1
    exec "$agents/tools/tcache" run --repo "$PWD" --tier "smoke$($impact && echo -impact)" -- bash "$PWD/tests/smoke.sh" "$@"
fi
start=$SECONDS
for script in setup.linux.sh hw.sh devices/*.sh devices/*/*.sh .devcontainer/*.sh tests/*.sh; do
    bash -n "$script"
done
modules=(tests.test_field tests.test_hw_contract tests.test_hosts tests.test_profiles tests.test_setup_repos
         tests.test_readme tests.test_agnostic tests.test_install_check tests.test_overlay)
if $impact; then
    [[ -x $agents/tools/timpact ]] && selected=$("$agents/tools/timpact" --repo "$PWD") || selected=ALL
    if ! grep -qx ALL <<<"$selected"; then
        modules=()
        while read -r test; do
            test=${test#"$PWD"/}
            if [[ $test == tests/*.py ]]; then test=${test%.py}; modules+=("${test//\//.}"); fi
        done <<<"$selected"
    fi
fi
if ((${#modules[@]})); then
    python3 -m unittest -q "${modules[@]}"
else
    echo 'no affected tests'
fi
echo "smoke OK in $((SECONDS - start)) s"
