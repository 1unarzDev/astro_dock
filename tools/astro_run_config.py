"""Static Linux/Jazzy startup projection. Installed astro tooling, no ROS imports."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import shutil
import tempfile
import yaml

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


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def render(defaults, selected, inherited, adapter, transport, output, platform):
    if platform != 'linux':
        raise ValueError('selected YAML supports Linux static off-boat only')
    result = compile_config(defaults, selected, inherited)
    output = Path(output).resolve()
    adapter, transport = Path(adapter), Path(transport)
    inputs = {'defaults.yaml': Path(defaults).read_bytes(),
              'selected.yaml': Path(selected).read_bytes(),
              'adapter.yaml': adapter.read_bytes(), 'fastdds.xml': transport.read_bytes()}
    projection = yaml.safe_load(inputs['adapter.yaml'])
    if not isinstance(projection.get('services', {}).get('dev'), dict):
        raise ValueError('adapter requires dev service')
    workspace = output.parent.parent.resolve()
    data_host = (workspace / result['effective']['data.root'][len(WS) + 1:]).resolve()
    if not data_host.is_relative_to(workspace):
        raise ValueError('data path escapes workspace through symlink')
    existing = data_host
    while not existing.exists():
        existing = existing.parent
    if not existing.is_dir() or not os.access(existing, os.W_OK):
        raise ValueError('data path needs writable workspace ancestor')
    parent = output.parent / 'run-generations'
    parent.mkdir(exist_ok=True)
    generation = Path(tempfile.mkdtemp(prefix='run-', dir=parent))
    container = WS + '/.devcontainer/run-generations/' + generation.name
    values = result['effective']
    environment = {'ROS_DOMAIN_ID': str(values['ros.domain_id']),
                   'ROS_AUTOMATIC_DISCOVERY_RANGE': values['ros.discovery_range'],
                   'MHSEALS_DATA': values['data.root'], 'ROS_WS': WS,
                   'RMW_IMPLEMENTATION': 'rmw_fastrtps_cpp',
                   'FASTDDS_DEFAULT_PROFILES_FILE': container + '/fastdds.xml',
                   'FASTRTPS_DEFAULT_PROFILES_FILE': container + '/fastdds.xml',
                   'ASTRO_RUN_GENERATION': container, 'ASTRO_RUN_MANAGED': '1',
                   'BASH_ENV': WS + '/.devcontainer/dev.bashrc'}
    result['environment'] = environment
    result['adapter_derivations'] = {k: projection['services']['dev'].get(k)
                                     for k in ('hostname', 'extra_hosts', 'group_add')}
    result['platform'] = platform
    result['inherited_adapter'] = {k: inherited[k] for k in
                                  ('ASTRO_HOSTNAME', 'DISPLAY') if k in inherited}
    dev = projection['services']['dev']
    dev.setdefault('environment', {}).update({k: v.replace('$', '$$')
                                             for k, v in environment.items()})
    payloads = {'effective.json': encoded(result), 'fastdds.xml': inputs['fastdds.xml'],
                'compose.yaml': yaml.safe_dump(projection, sort_keys=False).encode()}
    payloads.update({'source-' + k: v for k, v in inputs.items()})
    manifest = {'schema': 1, 'compiler': digest(Path(__file__).read_bytes()),
                'artifacts': {k: digest(v) for k, v in payloads.items()},
                'source_digests': {k: digest(v) for k, v in inputs.items()}}
    try:
        for name, data in {**payloads, 'manifest.json': encoded(manifest)}.items():
            (generation / name).write_bytes(data)
            (generation / name).chmod(0o444)
        verify(generation)
        temporary = output.with_suffix('.tmp')
        temporary.write_bytes(payloads['compose.yaml'])
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(generation)
        raise
    return generation


def verify(generation, inherited=None):
    generation = Path(generation)
    manifest = json.loads((generation / 'manifest.json').read_bytes())
    if manifest['schema'] != 1 or manifest['compiler'] != digest(Path(__file__).read_bytes()):
        raise ValueError('generation compiler identity mismatch')
    expected = {'effective.json', 'fastdds.xml', 'compose.yaml', 'source-defaults.yaml',
                'source-selected.yaml', 'source-adapter.yaml', 'source-fastdds.xml'}
    if set(manifest['artifacts']) != expected:
        raise ValueError('generation artifact inventory mismatch')
    for name, sha in manifest['artifacts'].items():
        if digest((generation / name).read_bytes()) != sha:
            raise ValueError('generation artifact digest mismatch: ' + name)
    for name, sha in manifest['source_digests'].items():
        if digest((generation / ('source-' + name)).read_bytes()) != sha:
            raise ValueError('generation source digest mismatch')
    environment = json.loads((generation / 'effective.json').read_bytes())['environment']
    if inherited is not None:
        for key, value in environment.items():
            if key in inherited and inherited[key] != value:
                raise ValueError('conflicting managed environment: ' + key)
        for key in (*FORBIDDEN[3:], 'ASTRO_ROS_DOMAIN_ID'):
            if key in inherited:
                raise ValueError('conflicting managed environment: ' + key)
    return environment


def catalog_bundle():
    """Explicit installed DescriptorBundle provider; no discovery or mutation rights."""
    from mhseals_config.catalog import DescriptorBundle
    resources = Path(sys.prefix) / 'share/astro-run-config'
    return DescriptorBundle.read_yaml((resources / 'run-catalog.yaml').read_text(),
        {'run-defaults': (resources / 'run-defaults.yaml').read_bytes()},
        {'astro.static-linux.compiler': compile_config}, {'astro.static-linux': '0.1.0'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('ASTRO_RUN_CONFIG'))
    parser.add_argument('--defaults')
    parser.add_argument('--adapter')
    parser.add_argument('--transport')
    parser.add_argument('--output')
    parser.add_argument('--platform', default='linux')
    parser.add_argument('--verify')
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    try:
        if args.verify:
            environment = verify(args.verify, None if args.restore else dict(os.environ))
            print('\n'.join(k + '\t' + v for k, v in environment.items()))
        elif args.config and args.defaults and args.output and args.adapter and args.transport:
            print(render(args.defaults, args.config, dict(os.environ), args.adapter,
                         args.transport, args.output, args.platform))
        else:
            parser.error('selection, defaults, adapter, transport and output required')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
