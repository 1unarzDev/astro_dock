#!/usr/bin/env python3
"""Static policy checks for the container setup; run by container-checks.yml."""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

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

# The dev image carries the tools boat_test needs to send bags off the boat.
packages = read(devcontainer / "packages.txt")
# The boat stack's runtime dependencies are baked in, never installed by hand into a container.
for pkg in ("rsync", "openssh-client", "python3-rich", "python3-libgpiod", "py_trees", "rosbridge_server",
            "rosapi", "spatio_temporal_voxel_layer", "mavros_extras"):
    require(re.search(rf"^{pkg}( |$)", packages, re.MULTILINE), f"packages: {pkg} missing from packages.txt")
# The RTCM forwarding needs a pyubx2 with PARSE_META (1.3.9); GNSS parsers are pinned.
requirements = read(devcontainer / "requirements.txt")
pyubx2 = re.search(r"^pyubx2==([0-9.]+)$", requirements, re.MULTILINE)
require(pyubx2 and tuple(map(int, pyubx2.group(1).split("."))) >= (1, 3, 9),
        "requirements: pin pyubx2 at 1.3.9 or later for PARSE_META")
require(re.search(r"^pyrtcm==[0-9.]+$", requirements, re.MULTILINE), "requirements: pin pyrtcm")

# rmem_max only caps a request; Fast DDS must ask for the raised size or sockets stay at rmem_default.
sysctl = re.search(r"rmem_max=(\d+)", read(root / "setup.linux.sh"))
sizes = [e.text for d in ET.parse(root / "config" / "fastdds.xml").getroot().iter()
         if d.tag.endswith("transport_descriptor") and d.findtext("{*}transport_id", "") == "mtu_udp"
         for e in d if e.tag.endswith("receiveBufferSize")]
require(sysctl and sizes == [sysctl.group(1)],
        "dds: config/fastdds.xml receiveBufferSize must equal setup.linux.sh's rmem_max")

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
# docker stop only reaches stop-children.sh if nothing replaces the compose command: Dev Containers
# swaps it for its own sleep wrapper whenever overrideCommand is true.
require(json.loads(read(devcontainer / "devcontainer.json")).get("overrideCommand") is False,
        "devcontainer: overrideCommand must be false or stop-children.sh never runs as PID 1")
require(dev.get("command") and dev["command"][-1].endswith("stop-children.sh"),
        "compose: dev command must be stop-children.sh")

# Jetson image architecture: native layered builds and validated promotion.
workflow = read(root / ".github/workflows/images.yml")
jetson = read(devcontainer / "Dockerfile.jetson")
general = read(devcontainer / "Dockerfile.dev")
boat_validate = read(devcontainer / "boat-validate.sh")
dockerfiles = "".join(read(path) for path in devcontainer.glob("Dockerfile*"))
jetson_code = "\n".join(line for line in jetson.splitlines() if not line.lstrip().startswith("#"))
for name in ("Dockerfile.dev", "Dockerfile.jetson"):
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
