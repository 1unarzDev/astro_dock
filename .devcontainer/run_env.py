"""Project config/run.yaml (+ operator run.yaml) into the Compose override's dev environment.

Run by prebuild.sh as `python3 -I`: strict YAML, four typed keys, no ROS imports.
"""
import argparse
import math
import os
from pathlib import Path, PurePosixPath
import sys
import yaml

WS = '/home/roboboat/roboboat_ws'
KEYS = {'ros.domain_id': int, 'ros.discovery_range': str, 'data.root': str, 'transport.mode': str}
ENV = {'ASTRO_ROS_DOMAIN_ID': 'ros.domain_id', 'ROS_DOMAIN_ID': 'ros.domain_id',
       'ROS_AUTOMATIC_DISCOVERY_RANGE': 'ros.discovery_range', 'MHSEALS_DATA': 'data.root'}
# Transport controls a shell overlay could otherwise keep alongside the managed profile.
FORBIDDEN = ('RMW_IMPLEMENTATION', 'FASTDDS_DEFAULT_PROFILES_FILE', 'FASTRTPS_DEFAULT_PROFILES_FILE',
             'FASTDDS_BUILTIN_TRANSPORTS', 'CYCLONEDDS_URI', 'ROS_LOCALHOST_ONLY',
             'ASTRO_DDS_SUBNET', 'ASTRO_DDS_ADDRESS')


class Loader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key, _ in node.value:
            if key.tag == 'tag:yaml.org,2002:merge':
                raise ValueError(f'{self.name}:{key.start_mark.line + 1}: merge keys are not allowed')
            name = self.construct_object(key, deep=True)
            if name in seen:
                raise ValueError(f'{self.name}:{key.start_mark.line + 1}: duplicate key {name!r}')
            seen.add(name)
        return super().construct_mapping(node, deep)


def load(path):
    text = path.read_text()
    for event in yaml.parse(text):
        if isinstance(event, yaml.AliasEvent) or getattr(event, 'anchor', None):
            raise ValueError(f'{path}:{event.start_mark.line + 1}: anchors and aliases are not allowed')
    loader = Loader(text)
    loader.name = str(path)
    try:
        data = loader.get_single_data()
    finally:
        loader.dispose()
    return {} if data is None else data


def flatten(data, path, prefix=''):
    if not isinstance(data, dict):
        raise ValueError(f'{path}: expected a mapping')
    for key, value in data.items():
        name = prefix + str(key)
        if isinstance(value, dict):
            yield from flatten(value, path, name + '.')
        elif name not in KEYS:
            raise ValueError(f'{path}: unknown key {name}')
        elif type(value) is not KEYS[name] or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError(f'{path}: {name} must be {KEYS[name].__name__}')
        else:
            yield name, value


def validate(values):
    if not 0 <= values['ros.domain_id'] <= 232:
        raise ValueError('domain must be 0..232')
    if values['ros.discovery_range'] not in ('OFF', 'LOCALHOST', 'SUBNET'):
        raise ValueError('unsupported discovery range')
    if values['transport.mode'] != 'static-fastdds':
        raise ValueError('unsupported transport mode')
    root = values['data.root']
    path = PurePosixPath(root)
    if '$' in root:
        raise ValueError('dollar-containing data paths are not supported')
    if (not root.startswith(WS + '/') or str(path) != root or '..' in path.parts
            or any(ord(c) < 32 or ord(c) == 127 for c in root)):
        raise ValueError('data root must be normalized beneath the workspace bind')


def resolve(files, env):
    """Layer files in order, validate that baseline, then apply explicit env overrides."""
    values = {}
    for path in files:
        values.update(flatten(load(path), path))
    missing = set(KEYS) - set(values)
    if missing:
        raise ValueError('missing keys: ' + ', '.join(sorted(missing)))
    validate(values)
    effective, seen = dict(values), set()
    for name, key in ENV.items():
        if name not in env:
            continue
        value = env[name]
        if not value or key in seen:
            raise ValueError('empty or ambiguous explicit environment: ' + name)
        seen.add(key)
        if key == 'ros.domain_id':
            if not value.isascii() or not value.isdecimal():
                raise ValueError('invalid domain environment: ' + name)
            value = int(value)
        effective[key] = value
    for name in FORBIDDEN:
        if name in env:
            raise ValueError('explicit transport override unsupported: ' + name)
    validate(effective)
    return effective


def project(values):
    return {'ROS_DOMAIN_ID': str(values['ros.domain_id']),
            'ROS_AUTOMATIC_DISCOVERY_RANGE': values['ros.discovery_range'],
            'MHSEALS_DATA': values['data.root'], 'RMW_IMPLEMENTATION': 'rmw_fastrtps_cpp',
            'FASTDDS_DEFAULT_PROFILES_FILE': WS + '/config/fastdds.xml',
            'FASTRTPS_DEFAULT_PROFILES_FILE': WS + '/config/fastdds.xml',
            'ASTRO_RUN_MANAGED': '1'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', action='append', default=[], type=Path)
    parser.add_argument('--adapter', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        env = project(resolve(args.file, os.environ))
        with open(args.adapter) as stream:
            adapter = yaml.safe_load(stream)
        dev = adapter['services']['dev']
        dev.setdefault('environment', {}).update({k: v.replace('$', '$$') for k, v in env.items()})
        with open(args.output, 'w') as stream:
            yaml.safe_dump(adapter, stream, sort_keys=False)
    except (ValueError, OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        sys.exit(f'run config: {exc}')


if __name__ == '__main__':
    main()
