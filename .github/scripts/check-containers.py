#!/usr/bin/env python3
"""Static policy checks for the container setup; run by container-checks.yml."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[2]
devcontainer = root / ".devcontainer"
failures: list[str] = []


def require(ok: object, message: str) -> None:
    if not ok:
        failures.append(message)


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# JetPack 6 builds ROS from an upstream release snapshot plus pinned overlays.
deps = read(devcontainer / "Dockerfile.deps")
release = re.search(r"^ARG ROS2_RELEASE=(release-[a-z]+-\d{8})$", deps, re.MULTILINE)
require(release, "deps: ROS2_RELEASE must pin an upstream release-<distro>-YYYYMMDD snapshot")
require("ros2/ros2/${ROS2_RELEASE}/ros2.repos" in deps, "deps: install_ros2.sh must use the ROS2_RELEASE snapshot")
require("ARG VELODYNE_REF=0f2a3bb1dde4fa91cbafad8a3f9f89b66c2a1350" in deps, "deps: pinned Velodyne overlay is missing")
require("/opt/mavros" not in deps and "ARG MAVROS_REF" not in deps, "deps: MAVROS belongs in core, not Jetson")
for path in ("packages.txt", "jetson-packages.txt"):
    # First field is the name; "name !distro" only skips it where it has no release.
    names = {line.split("#")[0].split()[0] for line in read(devcontainer / path).splitlines()
             if line.split("#")[0].strip()}
    require({"topic_tools", "vision_msgs"} <= names, f"packages: {path} must include topic_tools and vision_msgs")

# Dev Containers Compose: VS Code's local UID image must never be pulled.
with tempfile.NamedTemporaryFile("w", suffix=".yml") as overlay:
    overlay.write("services:\n  dev:\n    image: vsc-local-uid-image\n")
    overlay.flush()
    config = json.loads(subprocess.run(
        ["docker", "compose", "-f", str(devcontainer / "docker-compose.yml"), "-f", overlay.name,
         "config", "--format", "json"], check=True, capture_output=True, text=True).stdout)
dev = config["services"]["dev"]
require(dev.get("pull_policy", "missing") != "always",
        "compose: local UID images would be pulled from a registry")
require(f"{dev.get('hostname')}:{(dev.get('extra_hosts') or [''])[0]}" == "roboboat:roboboat=127.0.1.1",
        "compose: service hostname is not resolvable")
environment = dev.get("environment", {})
require(environment.get("ROS_AUTOMATIC_DISCOVERY_RANGE") == "SUBNET"
        and "ROS_LOCALHOST_ONLY" not in environment,
        "compose: deprecated ROS discovery environment")

# Jetson image architecture: native layered builds and validated promotion.
workflow = read(root / ".github/workflows/images.yml")
jetson = read(devcontainer / "Dockerfile.jetson")
general = read(devcontainer / "Dockerfile.dev")
boat_validate = read(devcontainer / "boat-validate.sh")
dockerfiles = "".join(read(path) for path in devcontainer.glob("Dockerfile*"))
jetson_code = "\n".join(line for line in jetson.splitlines() if not line.lstrip().startswith("#"))
for name in ("Dockerfile.dev", "Dockerfile.jetson", "Dockerfile.jetson-jp5"):
    require("/usr/local/sbin/image-user" in read(devcontainer / name),
            f"architecture: {name} must create its user with image-user.sh")
for ok, message in [
    (re.search(r"runs-on:.*ubuntu-24.04-arm", workflow), "the default ARM64 dependency build must run natively"),
    ("setup-qemu-action" not in workflow, "QEMU must not install or compile Jetson images"),
    (not re.search(r"\b(apt-get|apt |dpkg|rosdep|colcon)\b", jetson_code), "Dockerfile.jetson must remain a packaging-only layer"),
    ("ROSDEP_SKIP_KEYS" not in dockerfiles + workflow, "dependencies belong in the pinned dependency layer, not rosdep skip lists"),
    ("Dockerfile.deps" in workflow, "the native dependency layer is missing from image automation"),
    ("needs.deps.outputs.image" not in workflow, "registry image names must not cross jobs (GitHub redacts secret-bearing outputs)"),
    (re.search(r"DEPS_KEY:.*needs\.deps\.outputs\.key", workflow), "the Jetson build must verify the dependency content key"),
    ("deps-${DEPS_KEY}" in workflow, "the dependency tag must be checked against its content-key label"),
    (not re.search(r"\$\{\{ env\.REGISTRY_IMAGE \}\}:[a-z0-9-]+-\$\{\{ github\.sha \}\}", workflow), "runtime images must use bounded staging and rollback tags"),
    (re.search(r"registry-retention\.py.*rotate", workflow), "validated images must rotate through the retention helper"),
    (re.search(r"candidate.*REGISTRY_IMAGE", workflow), "Jetson candidates must be retained by digest in fixed slots"),
    ("configure-zed-access" in read(devcontainer / "image-user.sh"), "image-user.sh must grant SDK access through configure-zed-access.sh"),
    (not re.search(r"\[\[|< <\(", general), "Dockerfile.dev uses /bin/sh; Bash-only logic belongs in a helper"),
    ("grep -qw zed" not in workflow, "smoke tests must verify ZED access, not a vendor group name"),
    (not re.search(r"^RUN .*astro-smoke jetson", jetson, re.MULTILINE), "Jetson hardware smoke tests must not run during image assembly"),
    ("Starting privileged hardware validation container" in boat_validate, "Jetson candidates need the native hardware-validation path"),
    ("ZED_Diagnostic" in boat_validate, "native Jetson validation must check the ZED diagnostic"),
]:
    require(ok, f"architecture: {message}")

for failure in failures:
    print(f"container checks: {failure}", file=sys.stderr)
print(f"container checks: {'FAIL' if failures else 'pass'} ({release.group(1) if release else 'no release pin'})")
sys.exit(1 if failures else 0)
