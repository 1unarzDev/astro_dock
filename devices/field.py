"""Pure logic of hw.sh hot and station take (devices/field.sh): the boat link from `ip` output and
the build plan of a hot patch. Reads, never changes anything.

  field.py link SUBNET          BOAT_IFACE, BOAT_ADDR and UPLINK_IFACE as shell assignments
  field.py plan REPO            stdin: `git diff --name-status` lines; out: the packages to build (line
                                one, may be empty), then why, one line each
  field.py packages REPO        stdin: repo-relative paths; out: NAME<TAB>DIR of the packages holding
                                them, once each (hw.sh overlay)
"""
import ipaddress
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

# Live under colcon --symlink-install once they exist: edits need no build.
LIVE = re.compile(r'\.(py|ya?ml)$|(^|/)launch/')
# Build inputs even though they match LIVE.
BUILD_FILES = {'setup.py', 'setup.cfg', 'package.xml', 'CMakeLists.txt'}
# Docs and tests: never installed, so no change of theirs builds.
NO_BUILD = re.compile(r'(^|/)(README|CHANGELOG|LICENSE)[^/]*$|\.(md|rst)$|(^|/)(docs?|tests?)/')


class Error(Exception):
    pass


def ip_lines(env, *command):
    """Output lines of `ip` (tests: the env variable holds them, newline separated)."""
    if env in os.environ:
        return os.environ[env].splitlines()
    run = subprocess.run(['ip', *command], capture_output=True, text=True, timeout=5)
    return run.stdout.splitlines()


def addresses(lines):
    """[(interface, ip_interface)] from `ip -o -4 addr show` lines."""
    out = []
    for line in lines:
        words = line.split()
        if len(words) > 3 and words[2] == 'inet':
            out.append((words[1], ipaddress.ip_interface(words[3])))
    return out


def uplinks(lines):
    """The interfaces of the default routes, in order, from `ip -o -4 route show default` lines."""
    out = []
    for line in lines:
        words = line.split()
        if words[:1] == ['default'] and 'dev' in words[:-1]:
            dev = words[words.index('dev') + 1]
            out += [] if dev in out else [dev]
    return out


def boat_link(addr_lines, route_lines, subnet):
    """The one interface with an address in the vessel subnet, that address, and the default-route
    interface that is not it (the internet side; empty when there is none)."""
    net = ipaddress.ip_network(subnet, strict=False)
    found = [(dev, a) for dev, a in addresses(addr_lines) if a.ip in net]
    devs = list(dict.fromkeys(dev for dev, _ in found))
    if not devs:
        raise Error(f'no interface has an address in {net}: connect this computer to the boat LAN '
                    f'(cable or the boat Wi-Fi), wait for its address, then rerun')
    if len(devs) > 1:
        raise Error(f'several interfaces are on {net} ({" ".join(devs)}): disconnect all but one, then rerun')
    up = [dev for dev in uplinks(route_lines) if dev != devs[0]]
    return {'BOAT_IFACE': devs[0], 'BOAT_ADDR': str(found[0][1].ip), 'UPLINK_IFACE': up[0] if up else ''}


def package_of(repo, path):
    """The colcon package (package.xml name) holding path in repo, or None outside any package."""
    repo = Path(repo).resolve()
    here = (repo / path).parent
    while True:
        manifest = here / 'package.xml'
        if manifest.is_file():
            match = re.search(r'<name>\s*([^<\s]+)\s*</name>', manifest.read_text(errors='replace'))
            return match.group(1) if match else here.name
        if here == repo or repo not in here.parents:
            return None
        here = here.parent


def packages_of(repo, paths):
    """'NAME<TAB>DIR' of each package holding one of paths (repo-relative), once each, in order."""
    out, repo = [], Path(repo).resolve()
    for path in paths:
        name = package_of(repo, path) if path else None
        if name is None:
            continue
        here = (repo / path).parent
        while not (here / 'package.xml').is_file():
            here = here.parent
        line = f'{name}\t{here}'
        out += [] if line in out else [line]
    return out


def plan(lines, repo):
    """(packages to build, reasons) for name-status lines. An edit (M) of an existing .py/.yaml/launch
    file is live under symlink-install, docs and tests are not installed; anything else in a package
    builds that package."""
    packages, reasons = [], []
    for line in lines:
        status, _, path = line.rstrip('\n').partition('\t')
        if not path:
            continue
        package = package_of(repo, path)
        if package is None:
            continue
        live = status == 'M' and LIVE.search(path) and Path(path).name not in BUILD_FILES
        if not live and not NO_BUILD.search(path):
            packages += [] if package in packages else [package]
            reasons.append(f'{package}: {status} {path}')
    return packages, reasons


def main(command, *args):
    if command == 'link':
        found = boat_link(ip_lines('HW_IP_ADDR', '-o', '-4', 'addr', 'show'),
                          ip_lines('HW_IP_ROUTE', '-o', '-4', 'route', 'show', 'default'), args[0])
        print('\n'.join(f'{k}={shlex.quote(v)}' for k, v in found.items()))
    elif command == 'plan':
        packages, reasons = plan(sys.stdin.read().splitlines(), args[0])
        print(' '.join(packages))
        print('\n'.join(reasons))
    elif command == 'packages':
        print('\n'.join(packages_of(args[0], sys.stdin.read().splitlines())))
    else:
        raise Error('usage: field.py link SUBNET | plan REPO | packages REPO')


if __name__ == '__main__':
    try:
        main(*sys.argv[1:])
    except (Error, OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
