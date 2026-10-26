#!/usr/bin/env python3
"""Print a device profile YAML as shell assignments for devices/lib.sh: python3 profile.py FILE NETWORK_ENV.

Keys join with '_' under HW_ (upper case, other characters become '_'): devices.rocket.address is
HW_DEVICES_ROCKET_ADDRESS. Lists join with spaces; each mapping also gets HW_<PATH>__KEYS. A `$KEY`
takes the environment's value, else NETWORK_ENV's; an unknown `$KEY` fails. A variable already set
in the shell keeps its value, so HW_* in the environment overrides the YAML.
"""
import os
import re
import shlex
import sys

import yaml

KEY = re.compile(r'\$([A-Z][A-Z0-9_]*)')


def network(path):
    env = {}
    for line in open(path):
        key, sep, value = line.strip().partition('=')
        if sep and re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
            env[key] = ' '.join(shlex.split(value, comments=True))
    return env


def main(path, env_path):
    env = {**network(env_path), **os.environ}
    data = yaml.safe_load(open(path)) or {}
    out = []

    def expand(text, where):
        def one(match):
            if match.group(1) not in env:
                sys.exit('%s: %s: $%s is not set in %s' % (path, where, match.group(1), env_path))
            return env[match.group(1)]
        return KEY.sub(one, text)

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

    if not isinstance(data, dict):
        sys.exit('%s: expected a mapping' % path)
    walk('HW', data, path.rsplit('/', 1)[-1])
    print('\n'.join(out))


if __name__ == '__main__':
    main(*sys.argv[1:])
