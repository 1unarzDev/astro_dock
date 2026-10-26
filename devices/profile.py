#!/usr/bin/env python3
"""Layered device profiles for devices/lib.sh. A profile root (ASTRO_PROFILES, the profiles/ submodule)
holds one directory per profile: NAME/profile.yaml (extends: PARENT, kind: vessel|station, network_env:
PATH, registry: PATH; paths relative to that directory) and one KIND.yaml per device kind. The fleet is
the one profile without `extends` (and without `kind`); every other profile is a member, a vessel or a
station. The fleet's optional topology.yaml (nodes, links) and secrets.keys are not device kinds.

Layers, parent first: mappings merge key by key, lists and scalars replace. A child sets only keys its
parents define, except under a mapping the parent leaves empty ({}) or a key it leaves null (the child
fills those). A child may add a kind. YAML is strict (no duplicate keys, anchors, aliases or merge keys);
errors name FILE:LINE.

  profile.py resolve ROOT         load and check every profile, then pick this machine's host and vessel
                                  (shell assignments; see resolve() for the inputs)
  profile.py meta DIR             HW_PROFILE_CHAIN, HW_PROFILE_KINDS, and ASTRO_NETWORK_ENV /
                                  ASTRO_REGISTRY unless already set (shell assignments)
  profile.py kind DIR KIND ENV    KIND as HW_* assignments; a set HW_* wins; $KEY reads the
                                  environment, then ENV (an unknown $KEY fails)
  profile.py show DIR [ENV]       the merged profile as YAML; $KEY reads ENV only (default: the
                                  profile's own network env; else stays as written), so no secret shows
  profile.py roles REGISTRY HOST  the registry roles of HOST, one per line

Kind keys join with '_' under HW_ (upper case, other characters become '_'): devices.rocket.address is
HW_DEVICES_ROCKET_ADDRESS. Lists join with spaces; each mapping also gets HW_<PATH>__KEYS.
"""
import os
from pathlib import Path
import re
import shlex
import socket
import sys

import yaml

KEY = re.compile(r'\$([A-Z][A-Z0-9_]*)')
NAME = re.compile(r'[a-z0-9][a-z0-9_-]*')
META = ('extends', 'kind', 'network_env', 'registry')
KINDS = ('vessel', 'station')
NOT_KINDS = ('profile', 'topology')
IGNORED_HOSTNAMES = ('roboboat', 'local', 'localhost')    # the dev container's hostname, and placeholders
VIRTUAL = re.compile(r'(lo|docker.*|veth.*|br-.*)$')


class Error(Exception):
    pass


class Map(dict):
    """A mapping that remembers each key's line."""
    lines = {}


class Loader(yaml.SafeLoader):
    def map(self, node):
        lines = {}
        for key, _ in node.value:
            if key.tag == 'tag:yaml.org,2002:merge':
                raise Error(f'{self.name}:{key.start_mark.line + 1}: merge keys are not allowed')
            name = self.construct_object(key, deep=True)
            if name in lines:
                raise Error(f'{self.name}:{key.start_mark.line + 1}: duplicate key {name!r}')
            lines[name] = key.start_mark.line + 1
        out = Map(self.construct_mapping(node, deep=True))
        out.lines = lines
        return out


Loader.add_constructor('tag:yaml.org,2002:map', Loader.map)


def load(path):
    text = path.read_text()
    for event in yaml.parse(text):
        if isinstance(event, yaml.AliasEvent) or getattr(event, 'anchor', None):
            raise Error(f'{path}:{event.start_mark.line + 1}: anchors and aliases are not allowed')
    loader = Loader(text)
    loader.name = str(path)
    try:
        data = loader.get_single_data()
    except yaml.YAMLError as error:
        raise Error(f'{path}: {error}') from None
    finally:
        loader.dispose()
    if data is None:
        return Map()
    if not isinstance(data, Map):
        raise Error(f'{path}:1: expected a mapping')
    return data


def layer(here):
    """One profile's own profile.yaml, checked; paths made absolute (relative to its directory)."""
    file = here / 'profile.yaml'
    data = load(file) if file.exists() else Map()
    for key in data:
        if key not in META:
            raise Error(f'{file}:{data.lines[key]}: unknown key {key!r} (allowed: {", ".join(META)})')
        if not isinstance(data[key], str) or not data[key]:
            raise Error(f'{file}:{data.lines[key]}: {key} must be a non-empty string')
    if 'kind' in data and data['kind'] not in KINDS:
        raise Error(f'{file}:{data.lines["kind"]}: kind must be one of {", ".join(KINDS)}, not {data["kind"]!r}')
    if 'extends' in data and not NAME.fullmatch(data['extends']):
        raise Error(f'{file}:{data.lines["extends"]}: extends must name a profile, not {data["extends"]!r}')
    out = Map(data)
    out.lines = data.lines
    for key in ('network_env', 'registry'):
        if key in out:
            out[key] = os.path.normpath(here / out[key])
    return out


def chain(directory):
    """The profile directories, root first, and the merged profile.yaml (the nearest layer naming a path wins)."""
    dirs, meta, seen = [], {'network_env': None, 'registry': None}, []
    here = Path(directory)
    while True:
        here = here.resolve()
        if here in seen:
            raise Error('profile cycle: ' + ' -> '.join(p.name for p in [*seen, here]))
        seen.append(here)
        if not here.is_dir():
            raise Error(f'no profile at {here}')
        data = layer(here)
        dirs.insert(0, here)
        for key in ('network_env', 'registry'):
            if meta[key] is None:
                meta[key] = data.get(key)
        if 'extends' not in data:
            return dirs, meta
        parent = data['extends']
        file = here / 'profile.yaml'
        here = here.parent / parent
        if not here.is_dir():
            raise Error(f'{file}:{data.lines["extends"]}: no profile {parent!r}')


def merge(base, over, file, where=''):
    """over (a child's layer) on base (its parents'), refusing keys the parents do not define."""
    out = Map(base)
    out.lines = {}
    open_map = not base
    for key, value in over.items():
        at = f'{file}:{over.lines[key]}'
        name = where + str(key)
        if key not in base:
            if not open_map:
                raise Error(f'{at}: unknown key {name!r} (not in a parent profile)')
            out[key] = value
        elif base[key] is None or value is None:
            out[key] = value
        elif isinstance(base[key], dict) != isinstance(value, dict):
            raise Error(f'{at}: {name} must be {"a mapping" if isinstance(base[key], dict) else "a value or list"}')
        elif isinstance(value, dict):
            out[key] = merge(base[key], value, file, name + '.')
        else:
            out[key] = value
    return out


def kinds(dirs):
    """Device kinds: every YAML of the chain but profile.yaml, topology.yaml and a layer's own registry."""
    registries = {Path(p) for p in (layer(d).get('registry') for d in dirs) if p}
    return sorted({p.stem for d in dirs for p in d.glob('*.yaml')
                   if p.stem not in NOT_KINDS and p.resolve() not in {r.resolve() for r in registries}})


def resolve_kind(dirs, kind):
    data, found = Map(), False
    for d in dirs:
        file = d / (kind + '.yaml')
        if file.exists():
            data = merge(data, load(file), file) if found else load(file)
            found = True
    if not found:
        raise Error(f'profile {dirs[-1].name} has no {kind}.yaml')
    return data


def network(path):
    env = {}
    if not path:
        return env
    for line in open(path):
        key, sep, value = line.strip().partition('=')
        if sep and re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            env[key] = ' '.join(shlex.split(value, comments=True))
    return env


def expander(env, source, strict=True):
    def expand(text, where):
        def one(match):
            if match.group(1) not in env:
                if not strict:
                    return match.group(0)
                raise Error(f'{where}: ${match.group(1)} is not set in {source or "the environment (no network env)"}')
            return env[match.group(1)]
        return KEY.sub(one, text)
    return expand


def assignments(data, expand, kind):
    out = []

    def emit(name, value):
        out.append('[[ -v %s ]] || %s=%s' % (name, name, shlex.quote(value)))

    def walk(name, value, where):
        if isinstance(value, dict):
            emit(name + '__KEYS', ' '.join(str(k) for k in value))
            for key, item in value.items():
                walk(name + '_' + re.sub(r'[^A-Z0-9]', '_', str(key).upper()), item, '%s.%s' % (where, key))
        elif isinstance(value, list):
            emit(name, ' '.join(expand(str(v), where) for v in value))
        elif value is None:
            emit(name, '')
        else:
            emit(name, expand(str(value).lower() if isinstance(value, bool) else str(value), where))

    walk('HW', data, kind + '.yaml')
    return out


def expanded(value, expand, where):
    if isinstance(value, dict):
        return {k: expanded(v, expand, f'{where}.{k}') for k, v in value.items()}
    if isinstance(value, list):
        return [expanded(v, expand, where) for v in value]
    return expand(value, where) if isinstance(value, str) else value


def registry_hosts(path):
    hosts = load(Path(path)).get('hosts') or {}
    if not isinstance(hosts, dict):
        raise Error(f'{path}: hosts must be a mapping')
    return hosts


class Root:
    """Every profile under a root, loaded and checked: one broken file fails every command."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.profiles = {}
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and (d / 'profile.yaml').exists():
                if not NAME.fullmatch(d.name):
                    raise Error(f'{d}: a profile name is lower case letters, digits, _ and -')
                self.profiles[d.name] = layer(d)
        fleets = [n for n, data in self.profiles.items() if 'extends' not in data]
        if len(fleets) != 1:
            raise Error(f'{self.root}: expected one fleet profile (the one without extends), found '
                        + (' '.join(fleets) or 'none'))
        self.fleet = fleets[0]
        if 'kind' in self.profiles[self.fleet]:
            data = self.profiles[self.fleet]
            raise Error(f'{self.root / self.fleet}/profile.yaml:{data.lines["kind"]}: the fleet has no kind')
        self.members, self.chains, self.hosts, self.envs = {}, {}, {}, {}
        for name, data in self.profiles.items():
            dirs, meta = chain(self.root / name)
            if name != self.fleet:
                if 'kind' not in data:
                    raise Error(f'{self.root / name}/profile.yaml:1: a member needs kind: {" or ".join(KINDS)}')
                self.members[name] = data['kind']
            for kind in kinds(dirs):
                resolve_kind(dirs, kind)
            for key in ('network_env', 'registry'):
                if meta[key] and not Path(meta[key]).is_file():
                    raise Error(f'{self.root / name}/profile.yaml: {key} {meta[key]} does not exist')
            self.chains[name] = (dirs, meta)
            self.envs[name] = network(meta['network_env']) if meta['network_env'] else {}
            if data.get('registry'):                 # hosts belong to the member whose own file names the registry
                self.hosts[name] = registry_hosts(data['registry'])
        self.topology()

    def topology(self):
        file = self.root / self.fleet / 'topology.yaml'
        if not file.exists():
            return
        data = load(file)
        nodes = data.get('nodes') or Map()
        if not isinstance(nodes, dict) or dict(nodes) != self.members:
            raise Error(f'{file}:{data.lines.get("nodes", 1)}: nodes must list every member and its kind: '
                        + ', '.join(f'{n}: {k}' for n, k in self.members.items()))
        for i, link in enumerate(data.get('links') or []):
            where = f'{file}:{data.lines.get("links", 1)}: links[{i}]'
            ends = link.get('between') if isinstance(link, dict) else None
            if not isinstance(ends, list) or len(ends) != 2 or any(e not in self.members for e in ends):
                raise Error(f'{where}: between must name two members')
            via = link.get('via')
            if via is not None:
                member, _, kind = str(via).partition('.')
                if member not in self.members or kind not in kinds(self.chains[member][0]):
                    raise Error(f'{where}: via {via!r} must be MEMBER.KIND of an existing member and kind')

    def leases(self, member):
        """STATIC_LEASES of a member's network env: {hostname: mac}."""
        out = {}
        for entry in self.envs[member].get('STATIC_LEASES', '').split():
            mac, _, rest = entry.partition('=')
            out[rest.partition('=')[0]] = mac.lower()
        return out


def local_macs(sysfs):
    """Permanent (addr_assign_type 0) MACs of physical-looking interfaces: {mac: interface}."""
    out = {}
    base = Path(sysfs)
    for nic in sorted(base.iterdir()) if base.is_dir() else []:
        try:
            if VIRTUAL.match(nic.name) or (nic / 'addr_assign_type').read_text().strip() != '0':
                continue
            mac = (nic / 'address').read_text().strip().lower()
        except OSError:
            continue
        if mac and mac != '00:00:00:00:00:00':
            out[mac] = nic.name
    return out


def resolve(root_path):
    """Inputs (environment): ASTRO_HOST, ASTRO_VESSEL (overrides), HW_HOSTNAME and HW_SYSFS_NET (tests)."""
    root = Root(root_path)
    out = {'HW_PROFILES_ROOT': str(root.root), 'HW_FLEET': root.fleet,
           'HW_MEMBERS': ' '.join(f'{n}:{k}' for n, k in root.members.items())}
    hostname = os.environ.get('HW_HOSTNAME') or socket.gethostname().split('.')[0]
    macs = local_macs(os.environ.get('HW_SYSFS_NET', '/sys/class/net'))
    owners = {}                                                  # host -> members listing it
    for member, hosts in root.hosts.items():
        for host in hosts:
            owners.setdefault(host, []).append(member)
    host, how = os.environ.get('ASTRO_HOST', ''), ''
    if host:
        if host not in owners:
            raise Error(f'ASTRO_HOST/--host {host}: no registry lists it (hosts: {" ".join(sorted(owners))})')
        how = '--host/ASTRO_HOST'
    else:
        found = {}
        if hostname not in IGNORED_HOSTNAMES and hostname in owners:
            found[hostname] = f'hostname {hostname}'
        for member in root.hosts:
            for name, mac in root.leases(member).items():
                if mac in macs and name in owners:
                    found.setdefault(name, f'MAC {mac} ({macs[mac]})')
        if len(found) > 1:
            raise Error('this machine matches more than one host: '
                        + ', '.join(f'{h} by {w}' for h, w in sorted(found.items())) + '; pass --host')
        if found:
            host, how = next(iter(found.items()))
    out.update(ASTRO_HOST=host, HW_SELF_HOW=how or f'no match: hostname {hostname}; MACs checked: '
               f'{" ".join(sorted(macs)) or "none"}')
    out['HW_SELF_MEMBER'] = host if root.members.get(host) == 'station' else ''
    vessel = os.environ.get('ASTRO_VESSEL', '')
    scope_how, scope_error = '', ''
    if vessel:
        if root.members.get(vessel) != 'vessel':
            raise Error(f'ASTRO_VESSEL/--vessel {vessel}: not a vessel (vessels: '
                        + ' '.join(n for n, k in root.members.items() if k == 'vessel') + ')')
        scope_how = '--vessel/ASTRO_VESSEL'
    elif host:
        vessels = [m for m in owners[host] if root.members.get(m) == 'vessel']
        if len(vessels) > 1:
            raise Error(f'{host} is on vessels {" ".join(vessels)}; pass --vessel')
        if vessels:
            vessel, scope_how = vessels[0], f'registry of {vessels[0]} lists {host}'
    if not vessel:
        scope_error = (f'No vessel for this machine (hostname {hostname}; MACs checked: '
                       f'{" ".join(sorted(macs)) or "none"}; members: {out["HW_MEMBERS"]}); pass --vessel')
    dirs, meta = root.chains[vessel or root.fleet]
    out.update(ASTRO_VESSEL=vessel, HW_SCOPE_HOW=scope_how, HW_SCOPE_ERROR=scope_error,
               HW_PROFILE=str(dirs[-1]), HW_PROFILE_CHAIN=' '.join(d.name for d in dirs),
               HW_PROFILE_KINDS=' '.join(kinds(dirs)))
    lines = ['%s=%s' % (k, shlex.quote(v)) for k, v in out.items()]
    for key, var in (('network_env', 'ASTRO_NETWORK_ENV'), ('registry', 'ASTRO_REGISTRY')):
        value = (meta[key] or '') if vessel else ''
        lines.append('[[ -n ${%s:-} ]] || %s=%s' % (var, var, shlex.quote(value)))
    return lines


def main(command, *args):
    if command == 'resolve':
        print('\n'.join(resolve(args[0])))
    elif command == 'meta':
        dirs, meta = chain(args[0])
        print('HW_PROFILE_CHAIN=%s' % shlex.quote(' '.join(d.name for d in dirs)))
        print('HW_PROFILE_KINDS=%s' % shlex.quote(' '.join(kinds(dirs))))
        for key, var in (('network_env', 'ASTRO_NETWORK_ENV'), ('registry', 'ASTRO_REGISTRY')):
            print('[[ -n ${%s:-} ]] || %s=%s' % (var, var, shlex.quote(meta[key] or '')))
    elif command == 'kind':
        directory, kind, env_path = args
        dirs, _ = chain(directory)
        expand = expander({**network(env_path), **os.environ}, env_path)
        print('\n'.join(assignments(resolve_kind(dirs, kind), expand, kind)))
    elif command == 'show':
        dirs, meta = chain(args[0])
        env_path = args[1] if len(args) > 1 else (meta['network_env'] or '')
        expand = expander(network(env_path), env_path, strict=False)
        shown = {'profile': ' -> '.join(d.name for d in dirs), 'network_env': meta['network_env'],
                 'registry': meta['registry']}
        for kind in kinds(dirs):
            shown[kind] = expanded(dict(resolve_kind(dirs, kind)), expand, kind)
        print(yaml.safe_dump(shown, sort_keys=False, default_flow_style=False, width=110).rstrip())
    elif command == 'roles':
        registry, host = args
        entry = registry_hosts(registry).get(host) or {}
        print('\n'.join(str(r) for r in (entry.get('roles') or [])))
    else:
        raise Error('usage: profile.py resolve|meta|kind|show|roles ...')


if __name__ == '__main__':
    try:
        main(*sys.argv[1:])
    except (Error, OSError, TypeError, ValueError) as error:
        sys.exit(str(error))
