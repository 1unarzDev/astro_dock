#!/usr/bin/env python3
"""Layered device profiles for devices/lib.sh. A profile is a directory config/profiles/NAME/ with an
optional profile.yaml (extends: PARENT, network_env: PATH, registry: PATH; paths from the repo root)
and one KIND.yaml per device kind.

Layers, parent first: mappings merge key by key, lists and scalars replace. A child sets only keys its
parents define, except under a mapping the parent leaves empty ({}) or a key it leaves null (the child
fills those). A child may add a kind. YAML is strict (no duplicate keys, anchors, aliases or merge keys);
errors name FILE:LINE.

  profile.py meta DIR             HW_PROFILE_CHAIN, HW_PROFILE_KINDS, and ASTRO_NETWORK_ENV /
                                  ASTRO_REGISTRY unless already set (shell assignments)
  profile.py default PROFILES     the one profile whose profile.yaml names a network_env
  profile.py kind DIR KIND ENV    KIND as HW_* assignments; a set HW_* wins; $KEY reads the
                                  environment, then ENV (an unknown $KEY fails)
  profile.py show DIR [ENV]       the merged profile as YAML; $KEY reads ENV only (else stays as
                                  written), so no secret shows

Kind keys join with '_' under HW_ (upper case, other characters become '_'): devices.rocket.address is
HW_DEVICES_ROCKET_ADDRESS. Lists join with spaces; each mapping also gets HW_<PATH>__KEYS.
"""
import os
from pathlib import Path
import re
import shlex
import sys

import yaml

KEY = re.compile(r'\$([A-Z][A-Z0-9_]*)')
META = ('extends', 'network_env', 'registry')


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


def chain(directory):
    """The profile directories, root first, and the merged profile.yaml."""
    dirs, meta, seen = [], {}, []
    here = Path(directory)
    while True:
        here = here.resolve()
        if here in seen:
            raise Error('profile cycle: ' + ' -> '.join(p.name for p in [*seen, here]))
        seen.append(here)
        if not here.is_dir():
            raise Error(f'no profile at {here}')
        file = here / 'profile.yaml'
        data = load(file) if file.exists() else Map()
        for key in data:
            if key not in META:
                raise Error(f'{file}:{data.lines[key]}: unknown key {key!r} (allowed: {", ".join(META)})')
            if not isinstance(data[key], str) or not data[key]:
                raise Error(f'{file}:{data.lines[key]}: {key} must be a non-empty string')
        dirs.insert(0, here)
        for key in ('network_env', 'registry'):
            if meta.get(key) is None:
                meta[key] = data.get(key)       # the nearest layer naming it wins
        if 'extends' not in data:
            return dirs, meta
        parent = data['extends']
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', parent):
            raise Error(f'{file}:{data.lines["extends"]}: extends must name a profile, not {parent!r}')
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
    return sorted({p.stem for d in dirs for p in d.glob('*.yaml') if p.name != 'profile.yaml'})


def resolve(dirs, kind):
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


def main(command, *args):
    if command == 'meta':
        dirs, meta = chain(args[0])
        print('HW_PROFILE_CHAIN=%s' % shlex.quote(' '.join(d.name for d in dirs)))
        print('HW_PROFILE_KINDS=%s' % shlex.quote(' '.join(kinds(dirs))))
        for key, var in (('network_env', 'ASTRO_NETWORK_ENV'), ('registry', 'ASTRO_REGISTRY')):
            print('[[ -v %s ]] || %s=%s' % (var, var, shlex.quote(meta[key] or '')))
    elif command == 'default':
        named = [d.name for d in sorted(Path(args[0]).iterdir())
                 if (d / 'profile.yaml').exists() and 'network_env' in load(d / 'profile.yaml')]
        if len(named) != 1:
            raise Error('set ASTRO_PROFILE (environment or .env) or --profile to one of '
                        + ' '.join(sorted(d.name for d in Path(args[0]).iterdir() if d.is_dir())))
        print(named[0])
    elif command == 'kind':
        directory, kind, env_path = args
        dirs, _ = chain(directory)
        expand = expander({**network(env_path), **os.environ}, env_path)
        print('\n'.join(assignments(resolve(dirs, kind), expand, kind)))
    elif command == 'show':
        dirs, meta = chain(args[0])
        env_path = args[1] if len(args) > 1 else ''
        expand = expander(network(env_path), env_path, strict=False)
        shown ={'profile': ' -> '.join(d.name for d in dirs), 'network_env': meta['network_env'],
                 'registry': meta['registry']}
        for kind in kinds(dirs):
            shown[kind] = expanded(dict(resolve(dirs, kind)), expand, kind)
        print(yaml.safe_dump(shown, sort_keys=False, default_flow_style=False, width=110).rstrip())
    else:
        raise Error('usage: profile.py meta|default|kind|show ...')


if __name__ == '__main__':
    try:
        main(*sys.argv[1:])
    except (Error, OSError, TypeError) as error:
        sys.exit(str(error))
