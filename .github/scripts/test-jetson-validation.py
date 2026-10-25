#!/usr/bin/env python3
"""Exercise hardware-validation gates without attaching real boat hardware."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'example/astro@sha256:' + 'a' * 64


class JetsonValidationTest(unittest.TestCase):
    """Check host compatibility and promotion ordering at the Docker boundary."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.script = root / '.devcontainer' / 'boat-validate.sh'
        self.script.parent.mkdir()
        shutil.copyfile(ROOT / '.devcontainer/boat-validate.sh', self.script)
        self.log = root / 'commands'
        self.env = dict(os.environ, MOCK_LOG=str(self.log), MOCK_L4T='36',
                        MOCK_JETPACK='6', MOCK_FAIL_CAMERA='0')
        binaries = root / 'bin'
        binaries.mkdir()
        self.env['PATH'] = str(binaries) + os.pathsep + self.env['PATH']
        self.executable(binaries / 'head', '''#!/bin/sh
if [ "$2" = /etc/nv_tegra_release ]; then
    echo "# R$MOCK_L4T (release), REVISION: 4.1"
else
    exec /usr/bin/head "$@"
fi
''')
        self.executable(binaries / 'docker', '''#!/usr/bin/env python3
import os
import sys
args = sys.argv[1:]
with open(os.environ['MOCK_LOG'], 'a') as stream:
    stream.write(' '.join(args) + '\\n')
if args[:2] == ['image', 'inspect']:
    print('arm64' if 'Architecture' in args[3]
          else os.environ['MOCK_JETPACK'])
if args[0] == 'inspect' and args[-1] == 'roboboat_dev':
    print('astro_dock_devcontainer')
if args[:2] == ['volume', 'ls']:
    filters = ' '.join(args)
    for name in ('zed_resources', 'zed_settings'):
        if ('volume=' + name) in filters and 'project=astro_dock_devcontainer' in filters:
            print('astro_dock_devcontainer_' + name)
if args[0] == 'exec' and '-i' in args:
    sys.stdin.read()
    if os.environ['MOCK_FAIL_CAMERA'] == '1' and 'bash' in args:
        sys.exit(1)
''')
        retention = root / '.github/scripts/registry-retention.py'
        retention.parent.mkdir(parents=True)
        self.executable(retention, '''#!/bin/sh
echo "retention $*" >> "$MOCK_LOG"
''')

    @staticmethod
    def executable(path, content):
        """Create a substitute executable in the isolated test directory."""
        path.write_text(content)
        path.chmod(0o755)

    def run_validation(self, *options):
        """Run the real shell entry point with isolated external commands."""
        return subprocess.run(
            ['bash', str(self.script), *options, IMAGE], env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=10)

    def test_jp6_validates_and_rotates_its_family(self):
        result = self.run_validation('--promote')
        self.assertEqual(result.returncode, 0, result.stdout)
        commands = self.log.read_text()
        self.assertIn('source /opt/ros/jazzy', commands)
        self.assertIn('ROS_DOMAIN_ID=223', commands)
        self.assertIn('rotate jetson-jp6-jazzy', commands)
        self.assertIn('pkg prefix topic_tools', commands)
        self.assertIn('test -x /usr/local/zed/tools/ZED_Diagnostic', commands)
        self.assertIn('rm -f astro_boat_validate_', commands)

    def test_camera_reuses_compose_zed_volumes_and_skips_rosbridge(self):
        result = self.run_validation()
        self.assertEqual(result.returncode, 0, result.stdout)
        commands = self.log.read_text()
        run = next(line for line in commands.splitlines() if line.startswith('run -d'))
        self.assertIn('-v astro_dock_devcontainer_zed_resources:/usr/local/zed/resources', run)
        self.assertIn('-v astro_dock_devcontainer_zed_settings:/usr/local/zed/settings', run)
        self.assertNotIn('rosbridge', commands)

    def test_mismatched_image_never_starts_a_container(self):
        self.env['MOCK_JETPACK'] = '5'
        result = self.run_validation('--promote')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not match host', result.stdout)
        commands = self.log.read_text()
        self.assertNotIn('run -d', commands)
        self.assertNotIn('retention ', commands)

    def test_failed_camera_never_promotes_and_cleans_up(self):
        self.env['MOCK_FAIL_CAMERA'] = '1'
        result = self.run_validation('--promote')
        self.assertNotEqual(result.returncode, 0)
        commands = self.log.read_text()
        self.assertNotIn('retention ', commands)
        self.assertIn('rm -f astro_boat_validate_', commands)

    def test_non_jetson_host_is_rejected_before_pull(self):
        self.env['MOCK_L4T'] = '35'
        result = self.run_validation()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_mutable_candidate_is_rejected(self):
        result = subprocess.run(
            ['bash', str(self.script), 'example/astro:next'], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())


if __name__ == '__main__':
    unittest.main()
