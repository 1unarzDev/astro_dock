import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_run_config import ROOT, WS


class PrebuildTest(unittest.TestCase):
    def test_real_prebuild_and_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('.devcontainer', 'config', 'tools', 'src/mhseals_config'):
                shutil.copytree(ROOT / name, root / name,
                                ignore=shutil.ignore_patterns('run-tooling', 'run-generations', '*.egg-info', '__pycache__'))
            stubs = root / 'stubs'
            stubs.mkdir()
            for name, body in {'uname': 'case "$1" in -s) echo Linux;; -m) echo x86_64;; -n) echo testhost;; esac',
                               'getent': 'exit 0', 'nvidia-smi': 'exit 99'}.items():
                # Presence of nvidia-smi selects a different platform: omit it.
                if name == 'nvidia-smi':
                    continue
                path = stubs / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            for name in ('bash', 'dirname', 'mktemp', 'cp', 'sed', 'cut', 'rm', 'mv', 'python3', 'docker', 'mkdir'):
                (stubs / name).symlink_to('/usr/bin/' + name)
            selection = root / 'run.yaml'
            selection.write_text('ros: {domain_id: 73, discovery_range: LOCALHOST}\n'
                                 'data: {root: ' + WS + '/bags/experiment}')
            env = {'PATH': str(stubs), 'HOME': str(root),
                   'ASTRO_RUN_CONFIG': str(selection), 'DISPLAY': ':0'}
            run = subprocess.run(['bash', str(root / '.devcontainer/prebuild.sh')],
                                 env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            command = ['docker', 'compose', '-f', str(root / '.devcontainer/docker-compose.yml'),
                       '-f', str(root / '.devcontainer/docker-compose.override.yml'),
                       'config', '--format', 'json']
            result = subprocess.run(command, env={**env, 'ASTRO_ROS_DOMAIN_ID': '99'},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            dev = json.loads(result.stdout)['services']['dev']
            values = dev['environment']
            self.assertTrue((root / '.config/mhseals').is_dir())
            self.assertIn((str(root / '.config/mhseals'), '/home/roboboat/.config/mhseals'),
                          [(v['source'], v['target']) for v in dev['volumes']])
            self.assertEqual(values['ROS_DOMAIN_ID'], '73')
            self.assertEqual(values['ROS_AUTOMATIC_DISCOVERY_RANGE'], 'LOCALHOST')
            self.assertEqual(values['MHSEALS_DATA'], WS + '/bags/experiment')
            self.assertEqual(values['RMW_IMPLEMENTATION'], 'rmw_fastrtps_cpp')
            override = root / '.devcontainer/docker-compose.override.yml'
            previous = override.read_bytes()
            selection.write_text('ros: {domain_id: false}')
            bad = subprocess.run(['bash', str(root / '.devcontainer/prebuild.sh')],
                                 env=env, capture_output=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(override.read_bytes(), previous)
            # Unsupported adapters refuse before publishing too.
            (stubs / 'uname').write_text('#!/bin/sh\necho Darwin\n')
            bad_platform = subprocess.run(['bash', str(root / '.devcontainer/prebuild.sh')],
                                          env=env, capture_output=True)
            self.assertNotEqual(bad_platform.returncode, 0)
            self.assertEqual(override.read_bytes(), previous)
