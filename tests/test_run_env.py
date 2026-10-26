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
        """The vessel's network env names the domain (fixture a: 7), injected without PyYAML; every other
        host keeps the fleet default 42 (a stale adapter, no vessel, no submodule, macOS without PyYAML);
        ASTRO_ROS_DOMAIN_ID still wins when Compose starts."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('.devcontainer', 'config', 'devices'):
                shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy(ROOT / 'hw.sh', root / 'hw.sh')

            def stubs(name, system, python='/usr/bin/python3'):
                path = root / name
                path.mkdir()
                (path / 'uname').write_text('#!/bin/sh\ncase "$1" in -s) echo %s;; -m) echo x86_64;; -n) echo testhost;; esac\n'
                                            % system)
                (path / 'getent').write_text('#!/bin/sh\nexit 0\n')
                (path / 'python3').write_text('#!/bin/sh\nexec %s "$@"\n' % python)
                for stub in ('uname', 'getent', 'python3'):
                    (path / stub).chmod(0o755)
                for tool in ('bash', 'dirname', 'basename', 'mktemp', 'cp', 'sed', 'cut', 'rm', 'mv', 'docker',
                             'mkdir', 'grep', 'tr', 'awk'):
                    (path / tool).symlink_to('/usr/bin/' + tool)
                return str(path)
            # macOS: no PyYAML (an import of yaml fails).
            noyaml = root / 'python3-noyaml'
            noyaml.write_text('#!/bin/sh\n[ "$1" = -I ] && shift\nexec /usr/bin/python3 -I -c '
                              '\'import runpy, sys; sys.modules["yaml"] = None; sys.argv = sys.argv[1:]; '
                              'runpy.run_path(sys.argv[0], run_name="__main__")\' "$@"\n')
            noyaml.chmod(0o755)
            linux, mac = stubs('linux', 'Linux'), stubs('mac', 'Darwin', python=str(noyaml))
            env = {'PATH': linux, 'HOME': str(root), 'ASTRO_SECRETS': '/nonexistent', 'ASTRO_ENV_FILE': '/dev/null',
                   'ASTRO_PROFILES': str(ROOT / 'tests/data/profiles'), 'HW_HOSTNAME': 'x', 'HW_SYSFS_NET': '/nonexistent'}
            prebuild = str(root / '.devcontainer/prebuild.sh')
            override = root / '.devcontainer/docker-compose.override.yml'
            compose = ['docker', 'compose', '-f', str(root / '.devcontainer/docker-compose.yml'),
                       '-f', str(override), 'config', '--format', 'json']

            warnings = []

            def config(**extra):
                result = subprocess.run(compose, env={**env, **extra}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)['services']['dev']['environment']['ROS_DOMAIN_ID']

            def domain(vessel, **extra):
                run = subprocess.run(['bash', prebuild], env={**env, **vessel}, capture_output=True, text=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                warnings.append(run.stderr)
                return config(**extra)
            # A stale adapter (pulled, prebuild not rerun) has no domain: the fleet default.
            shutil.copy(root / '.devcontainer/docker-compose.override.linux.yml', override)
            self.assertEqual(config(), '42')
            self.assertEqual(domain({'ASTRO_VESSEL': 'a'}), '7')
            self.assertNotIn('WARNING', warnings[-1])
            self.assertEqual(domain({'ASTRO_VESSEL': 'a'}, ASTRO_ROS_DOMAIN_ID='99'), '99')
            self.assertEqual(domain({'ASTRO_VESSEL': 'b'}), '42')    # a vessel env without ROS_DOMAIN_ID: no warning
            self.assertNotIn('WARNING', warnings[-1])
            # No vessel (an operator laptop off the LAN, or --host without a vessel): 42, said, and the override wins.
            self.assertEqual(domain({}), '42')
            self.assertIn('WARNING: This host resolves no vessel (see ./hw.sh whoami): [ERROR] No vessel for this machine', warnings[-1])
            self.assertIn('ASTRO_ROS_DOMAIN_ID', warnings[-1])
            self.assertIn('ASTRO_VESSEL', warnings[-1])
            self.assertEqual(domain({}, ASTRO_ROS_DOMAIN_ID='5'), '5')
            # macOS: the mac adapter (its own environment block) takes the vessel's domain without PyYAML ...
            self.assertEqual(domain({'ASTRO_VESSEL': 'a', 'PATH': stubs('mac-yaml', 'Darwin')}), '7')
            # ... and without PyYAML hw.sh says so, and the fleet default stays.
            self.assertEqual(domain({'ASTRO_VESSEL': 'a', 'PATH': mac}), '42')
            self.assertIn('PyYAML is missing', warnings[-1])
            self.assertNotIn('not initialised', warnings[-1])
            # The profiles submodule declared but not initialised: a loud banner, the fleet default.
            (root / '.gitmodules').write_text('[submodule "profiles"]\n\tpath = profiles\n\turl = https://example.invalid/p\n')
            self.assertEqual(domain({'ASTRO_PROFILES': ''}), '42')
            self.assertIn('WARNING: The profiles submodule is declared but not initialised', warnings[-1])
            self.assertIn('ASTRO_ROS_DOMAIN_ID', warnings[-1])

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
