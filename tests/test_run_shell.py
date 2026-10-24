import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from astro_run_config import render, verify
from test_run_config import ROOT, WS


class ShellTest(unittest.TestCase):
    def test_actual_shells_frozen_and_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dev = root / '.devcontainer'
            dev.mkdir()
            shutil.copy(ROOT / '.devcontainer/dev.bashrc', dev / 'dev.bashrc')
            selection = root / 'run.yaml'
            selection.write_text('ros: {domain_id: 73, discovery_range: LOCALHOST}\n'
                                 'data: {root: ' + WS + '/bags/experiment}')
            adapter = root / 'adapter.yaml'
            adapter.write_text('services: {dev: {}}')
            generation = render(ROOT / 'config/run-defaults.yaml', selection, {}, adapter,
                                ROOT / 'config/fastdds.xml', dev / 'override.yml', 'linux')
            expected = verify(generation)
            self.assertEqual(expected['ROS_DOMAIN_ID'], '73')
            self.assertEqual(expected['MHSEALS_DATA'], WS + '/bags/experiment')
            self.assertEqual(expected['ROS_AUTOMATIC_DISCOVERY_RANGE'], 'LOCALHOST')
            composed = subprocess.run(['docker', 'compose', '-f',
                str(ROOT / '.devcontainer/docker-compose.yml'), '-f', str(dev / 'override.yml'),
                'config', '--format', 'json'], check=True, capture_output=True, text=True,
                env={'PATH': '/usr/bin:/bin', 'HOME': str(root)})
            compose_env = json.loads(composed.stdout)['services']['dev']['environment']
            for key, value in expected.items():
                self.assertEqual(compose_env[key], value)
            # Inert overlays deliberately clobber selected keys, then restoration wins.
            setup = root / 'setup.bash'
            setup.write_text('export ROS_DOMAIN_ID=3 RMW_IMPLEMENTATION=wrong MHSEALS_DATA=/tmp/wrong\n'
                             'printf overlay >> "$HOME/overlay.log"\n')
            stubs = root / 'bin'
            stubs.mkdir()
            for name in ('ros2', 'python3'):
                p = stubs / name
                p.write_text('#!/bin/sh\necho forbidden >&2; exit 89\n')
                p.chmod(0o755)
            rviz = stubs / 'rviz2'
            rviz.write_text('#!/bin/sh\n/usr/bin/env\n')
            rviz.chmod(0o755)
            installed_bin = str(Path(shutil.which('astro-run-config')).parent)
            env = {'HOME': WS, 'PATH': installed_bin + ':' + WS + '/bin:/usr/bin:/bin',
                   'ROS_DISTRO': 'jazzy', **expected}
            sandbox = ['bwrap', '--ro-bind', '/', '/', '--tmpfs', '/home',
                       '--dir', '/home/roboboat', '--bind', str(root), WS,
                       '--tmpfs', '/opt', '--dir', '/opt/ros/jazzy',
                       '--ro-bind', str(setup), '/opt/ros/jazzy/setup.bash', '--chdir', WS]
            def run(command, extra=None):
                return subprocess.run(sandbox + command, env={**env, **(extra or {})},
                                      capture_output=True, text=True)
            def assert_values(result):
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
                for key, value in expected.items():
                    self.assertEqual(actual[key], value, key)
                self.assertNotIn('forbidden', result.stderr)
            assert_values(run(['/usr/bin/env']))  # direct exec frozen Compose environment
            assert_values(run(['bash', '--noprofile', '--norc', '-c', '/usr/bin/env']))
            assert_values(run(['bash', '--noprofile', '--norc', '-ic',
                               'source "$BASH_ENV"; /usr/bin/env']))
            assert_values(run(['bash', '--noprofile', '--norc', '-c', 'rviz2']))
            selection.write_text('ros: {domain_id: 92}')
            assert_values(run(['bash', '--noprofile', '--norc', '-c', '/usr/bin/env']))
            overlay_before = (root / 'overlay.log').read_bytes()
            conflict = run(['bash', '--noprofile', '--norc', '-c', 'echo CHILD'], {'ROS_DOMAIN_ID': '4'})
            self.assertNotEqual(conflict.returncode, 0)
            self.assertNotIn('CHILD', conflict.stdout)
            self.assertEqual((root / 'overlay.log').read_bytes(), overlay_before)
            empty = run(['bash', '--noprofile', '--norc', '-c', 'echo CHILD'],
                        {'ASTRO_RUN_GENERATION': ''})
            self.assertNotEqual(empty.returncode, 0)
            self.assertNotIn('CHILD', empty.stdout)
            (generation / 'fastdds.xml').chmod(0o644)
            (generation / 'fastdds.xml').write_text('corrupt')
            for flags, command in [('-c', 'echo CHILD'), ('-ic', 'source "$BASH_ENV"; echo CHILD')]:
                bad = run(['bash', '--noprofile', '--norc', flags, command])
                self.assertNotEqual(bad.returncode, 0)
                self.assertNotIn('CHILD', bad.stdout)
            self.assertEqual((root / 'overlay.log').read_bytes(), overlay_before)
