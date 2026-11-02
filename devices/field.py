"""Pure logic of hw.sh hot and station take (devices/field.sh): the boat link from `ip` output, the
boat hosts of a registry and the build plan of a hot patch. Reads, never changes anything.

  field.py link SUBNET          BOAT_IFACE, BOAT_ADDR and UPLINK_IFACE as shell assignments
  field.py boat-hosts REGISTRY  the ssh hosts without the operator role, one per line
  field.py roles REGISTRY       every role the registry names (its roles: and the hosts' roles)
  field.py plan REPO            stdin: `git diff --name-status` lines; out: the packages to build (line
                                one, may be empty), then why, one line each
"""
import ipaddress
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import yaml

# Live under colcon --symlink-install once they exist: edits need no build.
LIVE = re.compile(r'\.(py|ya?ml)$|(^|/)launch/')
# Build inputs even though they match LIVE.
BUILD_FILES = {'setup.py', 'setup.cfg', 'package.xml', 'CMakeLists.txt'}


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


def registry(path):
    data = yaml.safe_load(Path(path).read_text()) or {}
    return data.get('defaults') or {}, data.get('hosts') or {}, data.get('roles') or {}


def boat_hosts(path):
    """The ssh hosts of a registry that are not operator stations, in registry order."""
    defaults, hosts, _ = registry(path)
    out = []
    for name, spec in hosts.items():
        spec = {**defaults, **(spec or {})}
        if spec.get('ssh', True) and 'operator' not in (spec.get('roles') or []):
            out.append(name)
    return out


def roles(path):
    _, hosts, defined = registry(path)
    out = list(defined)
    for spec in hosts.values():
        out += [str(r) for r in (spec or {}).get('roles') or [] if str(r) not in out]
    return out


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


def plan(lines, repo):
    """(packages to build, reasons) for name-status lines. An edit (M) of an existing .py/.yaml/launch
    file is live under symlink-install; anything else in a package builds that package."""
    packages, reasons = [], []
    for line in lines:
        status, _, path = line.rstrip('\n').partition('\t')
        if not path:
            continue
        package = package_of(repo, path)
        if package is None:
            continue
        live = status == 'M' and LIVE.search(path) and Path(path).name not in BUILD_FILES
        if not live:
            packages += [] if package in packages else [package]
            reasons.append(f'{package}: {status} {path}')
    return packages, reasons


def main(command, *args):
    if command == 'link':
        found = boat_link(ip_lines('HW_IP_ADDR', '-o', '-4', 'addr', 'show'),
                          ip_lines('HW_IP_ROUTE', '-o', '-4', 'route', 'show', 'default'), args[0])
        print('\n'.join(f'{k}={shlex.quote(v)}' for k, v in found.items()))
    elif command == 'boat-hosts':
        print('\n'.join(boat_hosts(args[0])))
    elif command == 'roles':
        print('\n'.join(roles(args[0])))
    elif command == 'plan':
        packages, reasons = plan(sys.stdin.read().splitlines(), args[0])
        print(' '.join(packages))
        print('\n'.join(reasons))
    else:
        raise Error('usage: field.py link SUBNET | boat-hosts REGISTRY | roles REGISTRY | plan REPO')


if __name__ == '__main__':
    try:
        main(*sys.argv[1:])
    except (Error, OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
