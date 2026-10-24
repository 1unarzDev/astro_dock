#!/usr/bin/env python3
"""Check installed configuration packages without importing ROS or starting nodes.

Run outside the source tree using the installation's Python environment. For
colcon, source only the intended ROS underlay and isolated install/local_setup.
"""
import argparse
import importlib
import importlib.metadata
import json
from pathlib import Path
import sys


def check_resources(prefix, name, origin):
    """Require exact resource names and contain every resolved installed path."""
    package_prefix = prefix / name if (prefix / name / 'share').is_dir() else prefix
    share = package_prefix / 'share' / name
    marker = package_prefix / 'share/ament_index/resource_index/packages' / name

    def require(path, directory=False):
        exists = path.is_dir() if directory else path.is_file()
        if not exists or not path.resolve().is_relative_to(prefix):
            raise SystemExit(f'{name}: missing or external installed resource {path}')
        return path.resolve()

    resources = [require(marker)]
    required = ['package.xml']
    if name == 'mhseals_nav':
        required += ['config/nav2_params.yaml', 'launch/robot.launch.py']
    for suffix in required:
        require(share / suffix)
    directories = {'mhseals_nav': ['behavior_trees'],
                   'mhseals_testing': ['config']}.get(name, [])
    for suffix in directories:
        directory = share / suffix
        require(directory, directory=True)
        if not any(path.is_file() for path in directory.rglob('*')):
            raise SystemExit(f'{name}: empty installed resource directory {directory}')
    resources += [require(path) for path in share.rglob('*') if path.is_file()]
    if name == 'mhseals_hardware':
        resources.append(require(origin.parent / 'config/default.yaml'))
    return resources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--include-testing', action='store_true')
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    packages = ['mhseals_config', 'mhseals_hardware', 'mhseals_nav']
    if args.include_testing:
        packages.append('mhseals_testing')
    report = {}
    for name in packages:
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            raise SystemExit(f'Required installed package {name} unavailable: {exc}') from exc
        pure_module = {'mhseals_config': 'mhseals_config.resolver',
                       'mhseals_hardware': 'mhseals_hardware.startup_config',
                       'mhseals_nav': 'mhseals_nav.startup_snapshot',
                       'mhseals_testing': 'mhseals_testing.configuration'}[name]
        importlib.import_module(pure_module)
        origin = Path(module.__file__).resolve()
        if not origin.is_relative_to(prefix):
            raise SystemExit(f'{name} imported outside installation {prefix}: {origin}')
        dist = importlib.metadata.distribution(name)
        resources = check_resources(prefix, name, origin)
        report[name] = {'version': dist.version, 'origin': str(origin),
                        'resources': [str(path) for path in sorted(resources)]}
    print(json.dumps({'python': sys.version, 'packages': report}, indent=2))


if __name__ == '__main__':
    main()
