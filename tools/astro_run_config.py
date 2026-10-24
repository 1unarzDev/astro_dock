"""Static Linux/Jazzy startup projection. Installed astro tooling, no ROS imports."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys

try:
    from mhseals_config.resolver import Assignment, Field, Origin, parse, resolve
except ImportError as exc:
    raise SystemExit('Install mhseals_config and astro-run-config in the tooling interpreter') from exc

WS = '/home/roboboat/roboboat_ws'
FIELDS = {'ros.domain_id': Field(int), 'ros.discovery_range': Field(str),
          'data.root': Field(str), 'transport.mode': Field(str)}
ENV = {'ASTRO_ROS_DOMAIN_ID': 'ros.domain_id', 'ROS_DOMAIN_ID': 'ros.domain_id',
       'ROS_AUTOMATIC_DISCOVERY_RANGE': 'ros.discovery_range', 'MHSEALS_DATA': 'data.root'}
FORBIDDEN = ('RMW_IMPLEMENTATION', 'FASTDDS_DEFAULT_PROFILES_FILE',
             'FASTRTPS_DEFAULT_PROFILES_FILE', 'FASTDDS_BUILTIN_TRANSPORTS',
             'CYCLONEDDS_URI', 'ROS_LOCALHOST_ONLY', 'ASTRO_DDS_SUBNET', 'ASTRO_DDS_ADDRESS')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def compile_config(defaults, selected, inherited):
    """Resolve explicit inputs before any adapter/shell defaults can intervene."""
    layers, sources = [], {}
    for layer, path in [('defaults', defaults), ('deployment', selected)]:
        path = Path(path).resolve()
        raw = path.read_bytes()
        sources[str(path)] = digest(raw)
        layers.append(parse(raw.decode(), str(path), layer, FIELDS))
    session, seen = [], set()
    for name, key in ENV.items():
        if name not in inherited:
            continue
        value = inherited[name]
        if not value or key in seen:
            raise ValueError('empty or ambiguous explicit environment: ' + name)
        seen.add(key)
        if key == 'ros.domain_id':
            if not value.isascii() or not value.isdecimal():
                raise ValueError('invalid domain environment: ' + name)
            value = int(value)
        session.append(Assignment(key, value, Origin(name, 0, 0, 'session')))
    for name in FORBIDDEN:
        if name in inherited:
            raise ValueError('explicit transport override unsupported: ' + name)
    if inherited.get('ROS_WS', WS) != WS or inherited.get('ROS_DISTRO', 'jazzy') != 'jazzy':
        raise ValueError('managed startup requires fixed workspace and Jazzy')
    result = resolve([*layers, session])
    values = dict(result.effective)
    if not 0 <= values['ros.domain_id'] <= 232:
        raise ValueError('domain must be 0..232')
    if values['ros.discovery_range'] not in ('OFF', 'LOCALHOST', 'SUBNET'):
        raise ValueError('unsupported discovery range')
    if values['transport.mode'] != 'static-fastdds':
        raise ValueError('unsupported transport mode')
    root = values['data.root']
    path = PurePosixPath(root)
    if (not root.startswith(WS + '/') or str(path) != root or '..' in path.parts
            or any(ord(c) < 32 or ord(c) == 127 for c in root)):
        raise ValueError('data root must be normalized beneath workspace bind')
    return {'baseline': dict(result.baseline), 'effective': values,
            'origins': {k: [asdict(o) for o in v] for k, v in result.origins.items()},
            'session': {k: inherited[k] for k in ENV if k in inherited}, 'sources': sources}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('ASTRO_RUN_CONFIG'))
    parser.add_argument('--defaults', required=True)
    args = parser.parse_args()
    if not args.config:
        parser.error('--config or ASTRO_RUN_CONFIG is required')
    try:
        print(json.dumps(compile_config(args.defaults, args.config, dict(os.environ)), indent=2))
    except (ValueError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
