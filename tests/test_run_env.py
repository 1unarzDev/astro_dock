import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.devcontainer'))
import run_env  # noqa: E402

WS = run_env.WS


class RunEnvTest(unittest.TestCase):
    def test_prebuild_projects_env_into_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('.devcontainer', 'config'):
                shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
            stubs = root / 'stubs'
            stubs.mkdir()
            (stubs / 'uname').write_text('#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo x86_64;; -n) echo testhost;; esac\n')
            (stubs / 'uname').chmod(0o755)
            (stubs / 'getent').write_text('#!/bin/sh\nexit 0\n')
            (stubs / 'getent').chmod(0o755)
            for name in ('bash', 'dirname', 'mktemp', 'cp', 'sed', 'cut', 'rm', 'mv', 'python3', 'docker', 'mkdir'):
                (stubs / name).symlink_to('/usr/bin/' + name)
            selection = root / 'run.yaml'
            selection.write_text('ros: {domain_id: 73, discovery_range: LOCALHOST}\n'
                                 'data: {root: ' + WS + '/bags/experiment}\n')
            env = {'PATH': str(stubs), 'HOME': str(root), 'ASTRO_RUN_CONFIG': str(selection)}
            prebuild = str(root / '.devcontainer/prebuild.sh')
            run = subprocess.run(['bash', prebuild], env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            compose = ['docker', 'compose', '-f', str(root / '.devcontainer/docker-compose.yml'),
                       '-f', str(root / '.devcontainer/docker-compose.override.yml'), 'config', '--format', 'json']
            result = subprocess.run(compose, env={**env, 'ASTRO_ROS_DOMAIN_ID': '99'}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = json.loads(result.stdout)['services']['dev']['environment']
            self.assertEqual(values['ROS_DOMAIN_ID'], '73')
            self.assertEqual(values['ROS_AUTOMATIC_DISCOVERY_RANGE'], 'LOCALHOST')
            self.assertEqual(values['MHSEALS_DATA'], WS + '/bags/experiment')
            self.assertEqual(values['RMW_IMPLEMENTATION'], 'rmw_fastrtps_cpp')
            override = root / '.devcontainer/docker-compose.override.yml'
            previous = override.read_bytes()
            selection.write_text('ros: {domain_id: false}\n')
            bad = subprocess.run(['bash', prebuild], env=env, capture_output=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(override.read_bytes(), previous)

    def test_prebuild_takes_the_vessel_domain(self):
        """The vessel's network env names the domain (fixture a: 7) for every adapter; without a vessel
        config/run.yaml's generic 0 stays; ASTRO_ROS_DOMAIN_ID still wins when Compose starts."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('.devcontainer', 'config', 'devices'):
                shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy(ROOT / 'hw.sh', root / 'hw.sh')
            stubs = root / 'stubs'
            stubs.mkdir()
            (stubs / 'uname').write_text('#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo x86_64;; -n) echo testhost;; esac\n')
            (stubs / 'uname').chmod(0o755)
            (stubs / 'getent').write_text('#!/bin/sh\nexit 0\n')
            (stubs / 'getent').chmod(0o755)
            for name in ('bash', 'dirname', 'basename', 'mktemp', 'cp', 'sed', 'cut', 'rm', 'mv', 'python3', 'docker',
                         'mkdir', 'grep', 'tr'):
                (stubs / name).symlink_to('/usr/bin/' + name)
            env = {'PATH': str(stubs), 'HOME': str(root), 'ASTRO_SECRETS': '/nonexistent', 'ASTRO_ENV_FILE': '/dev/null',
                   'ASTRO_PROFILES': str(ROOT / 'tests/data/profiles'), 'HW_HOSTNAME': 'x', 'HW_SYSFS_NET': '/nonexistent'}
            prebuild = str(root / '.devcontainer/prebuild.sh')
            compose = ['docker', 'compose', '-f', str(root / '.devcontainer/docker-compose.yml'),
                       '-f', str(root / '.devcontainer/docker-compose.override.yml'), 'config', '--format', 'json']

            def domain(vessel, **extra):
                run = subprocess.run(['bash', prebuild], env={**env, **vessel}, capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                result = subprocess.run(compose, env={**env, **extra}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)['services']['dev']['environment']['ROS_DOMAIN_ID']
            self.assertEqual(domain({'ASTRO_VESSEL': 'a'}), '7')
            self.assertEqual(domain({'ASTRO_VESSEL': 'a'}, ASTRO_ROS_DOMAIN_ID='99'), '99')
            self.assertEqual(domain({}), '0')                       # no vessel: the generic default

    def resolve(self, text, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'run.yaml'
            path.write_text(text)
            return run_env.resolve([ROOT / 'config/run.yaml', path], env or {})

    def test_strict_refusals(self):
        for text in ('ros: {domain_id: 1, domain_id: 2}', 'ros: &a {domain_id: 1}', 'ros: {<<: {domain_id: 1}}',
                     'ros: {domain_id: 1.5}', 'ros: {nope: 1}', 'data: {root: ' + WS + '/$HOME}',
                     'data: {root: ' + WS + '/../x}'):
            with self.subTest(text), self.assertRaises(ValueError):
                self.resolve(text)

    def test_overrides_and_baseline(self):
        self.assertEqual(self.resolve('{}', {'ROS_DOMAIN_ID': '81'})['ros.domain_id'], 81)
        for env in ({'ROS_DOMAIN_ID': '1', 'ASTRO_ROS_DOMAIN_ID': '2'}, {'MHSEALS_DATA': ''},
                    {'RMW_IMPLEMENTATION': 'x'}, {'ROS_DOMAIN_ID': '999'}):
            with self.subTest(env), self.assertRaises(ValueError):
                self.resolve('{}', env)
        # A session override cannot hide a bad baseline.
        with self.assertRaises(ValueError):
            self.resolve('ros: {domain_id: 999}', {'ROS_DOMAIN_ID': '5'})
