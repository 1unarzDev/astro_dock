#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workflow="$root/.github/workflows/images.yml"
final_image="$root/.devcontainer/Dockerfile.jetson"
general_image="$root/.devcontainer/Dockerfile.dev"

fail() {
    echo "image architecture check: $*" >&2
    exit 1
}

grep -q 'runs-on:.*ubuntu-24.04-arm' "$workflow" \
    || fail "the default ARM64 dependency build must run natively"

if grep -q 'setup-qemu-action' "$workflow"; then
    fail "QEMU must not execute package installation or compilation for Jetson images"
fi

if sed '/^[[:space:]]*#/d' "$final_image" | grep -Eq '\b(apt-get|apt |dpkg|rosdep|colcon)\b'; then
    fail "Dockerfile.jetson must remain a packaging-only layer"
fi

if grep -qs 'ROSDEP_SKIP_KEYS' "$root"/.devcontainer/Dockerfile* "$workflow"; then
    fail "local rosdep exception lists are not allowed; dependencies belong in the pinned dependency layer"
fi

grep -q 'Dockerfile.deps' "$workflow" \
    || fail "the native dependency layer is missing from image automation"

if grep -q 'needs\.deps\.outputs\.image' "$workflow"; then
    fail "registry image names must not cross jobs; GitHub may redact outputs containing registry secrets"
fi

grep -q 'DEPS_KEY:.*needs\.deps\.outputs\.key' "$workflow" \
    || fail "the Jetson build must reconstruct its dependency image from the content key"

grep -q 'Dependency job returned an invalid content key' "$workflow" \
    || fail "the Jetson build must reject an empty dependency image before invoking Buildx"

grep -q 'configure-zed-access' "$final_image" \
    || fail "the Jetson user must receive SDK access through the shared helper"

if grep -Eq '\[\[|< <\(' "$general_image"; then
    fail "Dockerfile.dev uses /bin/sh; Bash-only logic belongs in an explicit helper"
fi

grep -q 'configure-zed-access' "$general_image" \
    || fail "general images must use the shared optional ZED access helper"

if grep -q 'grep -qw zed' "$workflow"; then
    fail "image smoke tests must verify ZED access, not a vendor-specific group name"
fi

if grep -qE '^RUN .*astro-smoke jetson' "$final_image"; then
    fail "Jetson hardware smoke tests must not run while assembling the portable candidate"
fi

grep -q 'Starting privileged hardware validation container' "$root/.devcontainer/boat-validate.sh" \
    || fail "Jetson candidates must have an explicit native hardware-validation path"

grep -q 'ZED_Diagnostic' "$root/.devcontainer/boat-validate.sh" \
    || fail "native Jetson validation must check the ZED diagnostic"

echo "image architecture check: pass"
