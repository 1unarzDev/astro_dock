#!/usr/bin/env python3
"""Validate that the source-built ROS foundation cannot follow moving refs."""

from __future__ import annotations

import pathlib
import re
import sys


root = pathlib.Path(__file__).resolve().parent
manifest = (root / "ros2-jazzy.lock.repos").read_text(encoding="utf-8")
dockerfile = (root / "Dockerfile.deps").read_text(encoding="utf-8")

versions = re.findall(r"^[ ]{4}version: ([0-9a-f]+)$", manifest, re.MULTILINE)
invalid = [version for version in versions if not re.fullmatch(r"[0-9a-f]{40}", version)]
if len(versions) < 100 or invalid:
    print(
        f"dependency lock check: expected exact commits; found {len(versions)} entries and {len(invalid)} invalid refs",
        file=sys.stderr,
    )
    sys.exit(1)

rcutils = re.search(
    r"^  ros2/rcutils:.*?^[ ]{4}version: ([0-9a-f]{40})$",
    manifest,
    re.MULTILINE | re.DOTALL,
)
if not rcutils or rcutils.group(1) != "4cff430e271981b2e634c7c0c905f803beafb01d":
    print("dependency lock check: rcutils/rmw compatibility pin changed", file=sys.stderr)
    sys.exit(1)

if "COPY ros2-jazzy.lock.repos /tmp/ros2.repos" not in dockerfile:
    print("dependency lock check: Dockerfile.deps does not consume the lock", file=sys.stderr)
    sys.exit(1)

print(f"dependency lock check: pass ({len(versions)} exact commits)")
