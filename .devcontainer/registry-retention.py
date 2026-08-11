#!/usr/bin/env python3
"""Bound Docker Hub tag history without sacrificing validated rollbacks."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


FIXED_TAGS = {
    "core", "core-prev", "core-old", "core-next", "core-amd64", "core-arm64",
    "cuda", "cuda-prev", "cuda-old", "cuda-next",
    "sitl", "sitl-prev", "sitl-old", "sitl-next",
    "jetson", "jetson-prev", "jetson-old", "jetson-next", "jetson-check",
    "jetson-c1", "jetson-c2", "jetson-c3", "jetson-c4", "jetson-c5",
    "deps", "deps-prev", "deps-next", "cache-deps",
}
LEGACY_TAG = re.compile(
    r"^(?:(?:core(?:-(?:amd64|arm64))?|cuda|sitl|jetson|shaderc)-[0-9a-f]{40}"
    r"|deps-[0-9a-f]{40,64}|shaderc|buildcache-shaderc)$"
)


@dataclass(frozen=True)
class Tag:
    name: str
    digest: str
    updated: dt.datetime
    size: int


def utc(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def split_repository(repository: str) -> tuple[str, str]:
    if repository.count("/") != 1:
        raise ValueError("repository must be in namespace/name form")
    return tuple(repository.split("/", 1))  # type: ignore[return-value]


def request(url: str, *, method: str = "GET", token: str = "", body: Any = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"{method} {url} returned {error.code}: {detail}") from error


def inventory(repository: str, *, require_complete: bool = False) -> list[Tag]:
    namespace, name = split_repository(repository)
    base_url = f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{name}/tags"
    page_number = 1
    page_size = 100
    reported_count: int | None = None
    tags: list[Tag] = []
    while True:
        url = f"{base_url}?page={page_number}&page_size={page_size}"
        try:
            page = request(url)
        except RuntimeError as error:
            # Hub's count is eventually consistent after bulk deletion. It can
            # advertise another page even after every remaining tag was
            # returned, then answer its own next-page URL with 404.
            if page_number > 1 and "returned 404:" in str(error) and not require_complete:
                print(
                    "[retention] warning: Docker Hub advertised a stale trailing page; "
                    "using the tags already returned",
                    file=sys.stderr,
                )
                break
            raise
        if reported_count is None:
            reported_count = int(page.get("count") or 0)
        results = page["results"]
        tags.extend(
            Tag(
                name=item["name"],
                digest=item.get("digest", ""),
                updated=utc(item["last_updated"]),
                size=int(item.get("full_size") or 0),
            )
            for item in results
        )
        if len(results) < page_size or not page.get("next"):
            break
        page_number += 1

    if reported_count is not None and reported_count != len(tags):
        message = (
            f"Docker Hub reported {reported_count} tags but returned {len(tags)}; "
            "the count is probably still converging after tag deletion"
        )
        if require_complete:
            raise RuntimeError(message)
        print(f"[retention] warning: {message}", file=sys.stderr)
    return tags


def repository_size(repository: str) -> int:
    namespace, name = split_repository(repository)
    metadata = request(
        f"https://hub.docker.com/v2/namespaces/{namespace}/repositories/{name}"
    )
    return int(metadata.get("storage_size") or 0)


def run(*args: str) -> str:
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE).stdout


def resolve_digest(repository: str, source: str) -> str:
    if "@sha256:" in source:
        return source.rsplit("@", 1)[1]
    reference = source if "/" in source else f"{repository}:{source}"
    output = run("docker", "buildx", "imagetools", "inspect", reference)
    match = re.search(r"^Digest:\s+(sha256:[0-9a-f]{64})$", output, re.MULTILINE)
    if not match:
        raise RuntimeError(f"could not resolve a digest for {reference}")
    return match.group(1)


def retag(repository: str, tag: str, digest: str) -> None:
    print(f"[retention] {tag} <- {digest}")
    run(
        "docker", "buildx", "imagetools", "create",
        "--tag", f"{repository}:{tag}", f"{repository}@{digest}",
    )


def credentials() -> tuple[str, str]:
    username = os.environ.get("DOCKERHUB_USERNAME", "")
    secret = os.environ.get("DOCKERHUB_TOKEN", "")
    if not username or not secret:
        raise RuntimeError("DOCKERHUB_USERNAME and DOCKERHUB_TOKEN are required")
    return username, secret


def registry_token(repository: str) -> str:
    username, secret = credentials()
    query = urllib.parse.urlencode({
        "service": "registry.docker.io",
        "scope": f"repository:{repository}:pull,push,delete",
    })
    basic = base64.b64encode(f"{username}:{secret}".encode()).decode()
    req = urllib.request.Request(
        f"https://auth.docker.io/token?{query}",
        headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        payload = json.load(response)
    return payload.get("token") or payload["access_token"]


def delete_unreferenced(repository: str, digest: str) -> None:
    if not digest:
        return
    references = [tag.name for tag in inventory(repository) if tag.digest == digest]
    if references:
        print(f"[retention] keeping {digest}; referenced by {', '.join(sorted(references))}")
        return
    if not os.environ.get("DOCKERHUB_USERNAME") or not os.environ.get("DOCKERHUB_TOKEN"):
        print(f"[retention] no API credentials; leaving unreferenced manifest {digest}")
        return
    try:
        token = registry_token(repository)
        url = f"https://registry-1.docker.io/v2/{repository}/manifests/{digest}"
        request(url, method="DELETE", token=token)
        print(f"[retention] deleted unreferenced manifest {digest}")
    except RuntimeError as error:
        if "returned 404:" in str(error):
            print(f"[retention] manifest already absent: {digest}")
            return
        # Promotion has already succeeded at this point. Cleanup must never
        # turn a valid image publication into a failed deployment.
        print(f"[retention] warning: could not delete {digest}: {error}", file=sys.stderr)


def hub_token() -> str:
    username, secret = credentials()
    payload = request(
        "https://hub.docker.com/v2/auth/token",
        method="POST",
        body={"identifier": username, "secret": secret},
    )
    return payload.get("access_token") or payload["token"]


def delete_legacy_tag(repository: str, tag: str, token: str) -> None:
    namespace, name = split_repository(repository)
    # Docker Hub currently accepts this endpoint, but it is intentionally
    # isolated here because it is absent from the published Hub OpenAPI spec.
    url = f"https://hub.docker.com/v2/repositories/{namespace}/{name}/tags/{tag}/"
    request(url, method="DELETE", token=token)
    print(f"[retention] deleted legacy tag {tag}")


def probe_tag_deletion(repository: str) -> None:
    tags = inventory(repository)
    source = next((tag for tag in tags if tag.name == "core"), None)
    if not source:
        raise RuntimeError("cannot test tag deletion because the core tag is missing")
    probe = "retention-delete-probe"
    retag(repository, probe, source.digest)
    delete_legacy_tag(repository, probe, hub_token())
    if any(tag.name == probe for tag in inventory(repository)):
        raise RuntimeError("Docker Hub accepted probe deletion but the probe tag remains")
    print("[retention] Docker Hub tag-deletion probe passed")


def rotate(repository: str, family: str, source: str, depth: int) -> None:
    if depth not in (2, 3):
        raise ValueError("rotation depth must be 2 or 3")
    source_digest = resolve_digest(repository, source)
    existing = {tag.name: tag for tag in inventory(repository)}
    current = existing.get(family)
    previous = existing.get(f"{family}-prev")
    evicted = existing.get(f"{family}-old") if depth == 3 else previous

    if depth == 3 and previous:
        retag(repository, f"{family}-old", previous.digest)
    if current:
        retag(repository, f"{family}-prev", current.digest)
    retag(repository, family, source_digest)

    retained = {source_digest, current.digest if current else ""}
    if depth == 3 and previous:
        retained.add(previous.digest)
    if evicted and evicted.digest not in retained:
        delete_unreferenced(repository, evicted.digest)


def candidate_slot(repository: str, source: str, count: int) -> tuple[str, str]:
    source_digest = resolve_digest(repository, source)
    tags = inventory(repository)
    existing = {tag.name: tag for tag in tags}
    stable_digest = existing.get("jetson", Tag("", "", dt.datetime.min.replace(tzinfo=dt.timezone.utc), 0)).digest
    slots = [f"jetson-c{index}" for index in range(1, count + 1)]
    missing = [name for name in slots if name not in existing]
    reusable = [existing[name] for name in slots if name in existing and existing[name].digest == stable_digest]
    if missing:
        selected = missing[0]
        evicted = None
    elif reusable:
        selected = min(reusable, key=lambda tag: tag.updated).name
        evicted = existing[selected]
    else:
        selected = min((existing[name] for name in slots), key=lambda tag: tag.updated).name
        evicted = existing[selected]

    retag(repository, selected, source_digest)
    if evicted and evicted.digest != source_digest:
        delete_unreferenced(repository, evicted.digest)
    print(f"slot={selected}")
    print(f"digest={source_digest}")
    return selected, source_digest


def prune_candidates(repository: str, count: int, days: int) -> None:
    tags = inventory(repository)
    existing = {tag.name: tag for tag in tags}
    slots = [existing[name] for name in (f"jetson-c{i}" for i in range(1, count + 1)) if name in existing]
    if not slots:
        return
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    stable = existing.get("jetson")
    newest = max(slots, key=lambda tag: tag.updated)
    replacement = stable or newest
    evicted: set[str] = set()
    for slot in slots:
        if slot.updated < cutoff and slot.digest != replacement.digest:
            retag(repository, slot.name, replacement.digest)
            evicted.add(slot.digest)
    for digest in evicted:
        delete_unreferenced(repository, digest)


def migration_deletions(tags: list[Tag]) -> list[str]:
    return sorted(tag.name for tag in tags if tag.name not in FIXED_TAGS and LEGACY_TAG.fullmatch(tag.name))


def migrate(repository: str, apply: bool) -> None:
    # Destructive tag deletion is allowed only from a complete, internally
    # consistent inventory. Post-cleanup reads intentionally use the tolerant
    # mode because Docker Hub's count can remain stale for several minutes.
    tags = inventory(repository, require_complete=apply)
    existing = {tag.name: tag for tag in tags}

    candidates = sorted(
        (tag for tag in tags if re.fullmatch(r"jetson-[0-9a-f]{40}", tag.name)),
        key=lambda tag: tag.updated,
        reverse=True,
    )[:5]
    deps = sorted(
        (tag for tag in tags if re.fullmatch(r"deps-[0-9a-f]{40,64}", tag.name)),
        key=lambda tag: tag.updated,
        reverse=True,
    )
    deletions = migration_deletions(tags)
    for index, tag in enumerate(candidates, 1):
        print(f"KEEP jetson-c{index} <- {tag.name} ({tag.digest})")
    current_deps = existing.get("deps")
    older_deps = next((tag for tag in deps if not current_deps or tag.digest != current_deps.digest), None)
    if older_deps:
        print(f"KEEP deps-prev <- {older_deps.name} ({older_deps.digest})")
    print(f"[retention] migration would delete {len(deletions)} legacy tags")
    for name in deletions:
        print(f"DELETE {name}")
    if not apply:
        print("[retention] dry run; pass --apply to mutate Docker Hub")
        return

    probe_tag_deletion(repository)
    for index, tag in enumerate(candidates, 1):
        retag(repository, f"jetson-c{index}", tag.digest)
    if older_deps:
        retag(repository, "deps-prev", older_deps.digest)

    token = hub_token()
    for name in deletions:
        delete_legacy_tag(repository, name, token)


def audit(repository: str) -> None:
    tags = inventory(repository)
    unknown = sorted(tag.name for tag in tags if tag.name not in FIXED_TAGS)
    print(f"tags={len(tags)}")
    print(f"unique_manifests={len({tag.digest for tag in tags if tag.digest})}")
    print(f"storage_bytes={repository_size(repository)}")
    print(f"unexpected_tags={len(unknown)}")
    for name in unknown:
        print(f"UNEXPECTED {name}")


def snapshot(repository: str, output: str) -> None:
    tags = inventory(repository)
    payload = {
        "repository": repository,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "storage_bytes": repository_size(repository),
        "tags": [
            {
                "name": tag.name,
                "digest": tag.digest,
                "last_updated": tag.updated.isoformat(),
                "full_size": tag.size,
            }
            for tag in tags
        ],
    }
    with open(output, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(f"[retention] wrote {len(tags)} tags to {output}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--repository", default="lunarzdev/astro")
    commands = result.add_subparsers(dest="command", required=True)
    rotate_parser = commands.add_parser("rotate")
    rotate_parser.add_argument("family", choices=("core", "cuda", "sitl", "jetson", "deps"))
    rotate_parser.add_argument("source")
    rotate_parser.add_argument("--depth", type=int, choices=(2, 3), default=3)
    candidate_parser = commands.add_parser("candidate")
    candidate_parser.add_argument("source")
    candidate_parser.add_argument("--count", type=int, default=5)
    prune_parser = commands.add_parser("prune")
    prune_parser.add_argument("--count", type=int, default=5)
    prune_parser.add_argument("--days", type=int, default=30)
    migrate_parser = commands.add_parser("migrate")
    migrate_parser.add_argument("--apply", action="store_true")
    snapshot_parser = commands.add_parser("snapshot")
    snapshot_parser.add_argument("output")
    commands.add_parser("audit")
    return result


def main() -> None:
    args = parser().parse_args()
    if args.command == "rotate":
        rotate(args.repository, args.family, args.source, args.depth)
    elif args.command == "candidate":
        candidate_slot(args.repository, args.source, args.count)
    elif args.command == "prune":
        prune_candidates(args.repository, args.count, args.days)
        audit(args.repository)
    elif args.command == "migrate":
        migrate(args.repository, args.apply)
    elif args.command == "snapshot":
        snapshot(args.repository, args.output)
    else:
        audit(args.repository)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"registry retention failed: {error}", file=sys.stderr)
        raise SystemExit(1)
