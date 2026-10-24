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
        # pip installs share directly; default colcon uses one prefix per package.
        package_prefix = prefix / name if (prefix / name / 'share').is_dir() else prefix
        share = package_prefix / 'share' / name
        marker = package_prefix / 'share/ament_index/resource_index/packages' / name
        resources = sorted(path.resolve() for path in share.rglob('*') if path.is_file())
        if not marker.is_file() or not resources or not all(
                path.is_relative_to(prefix) for path in resources):
            raise SystemExit(f'{name}: missing or external installed share resources')
        resources.append(marker.resolve())
        required = {'mhseals_config': ['package.xml'],
                    'mhseals_hardware': ['package.xml'],
                    'mhseals_nav': ['config/nav2_params.yaml', 'launch/robot.launch.py'],
                    'mhseals_testing': ['config/']}[name]
        for suffix in required:
            if not any(f'share/{name}/{suffix}' in str(path) for path in resources):
                raise SystemExit(f'{name}: missing installed {suffix}')
        if name == 'mhseals_hardware':
            default = origin.parent / 'config/default.yaml'
            if not default.is_file():
                raise SystemExit(f'{name}: missing package data {default}')
            resources.append(default)
        if name == 'mhseals_nav' and not any('/behavior_trees/' in str(p) for p in resources):
            raise SystemExit(f'{name}: missing installed behavior trees')
        report[name] = {'version': dist.version, 'origin': str(origin),
                        'resources': [str(path) for path in sorted(resources)]}
    print(json.dumps({'python': sys.version, 'packages': report}, indent=2))


if __name__ == '__main__':
    main()
