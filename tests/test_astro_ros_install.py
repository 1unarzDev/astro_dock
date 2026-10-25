from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# apt-cache knows one ROS deb and one apt package; ros2 finds rclpy, and any
# package once apt-get installed something; pip only echoes.
STUBS = {
    'apt-cache': 'case "$2" in ros-jazzy-nav2-bringup|htop) exit 0;; *) exit 1;; esac',
    'apt-get': 'echo "apt-get $*"; [ "$1" = install ] && touch "$FAKE/installed"; exit 0',
    'ros2': '[ "$3" = rclpy ] || [ -f "$FAKE/installed" ]',
}


class InstallRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        for d in ('bin', 'ws/.devcontainer', 'venv/bin', 'lock'):
            (t / d).mkdir(parents=True)
        for name, body in dict(STUBS, **{'../venv/bin/pip': 'echo "pip $*"'}).items():
            (t / 'bin' / name).write_text('#!/bin/sh\n' + body + '\n')
            (t / 'bin' / name).chmod(0o755)
        # Root-only paths move into the temporary directory.
        script = (ROOT / '.devcontainer' / 'astro-ros-install.sh').read_text()
        for old, new in (('/var/lock', str(t / 'lock')), ('/workspace/venv', str(t / 'venv')),
                         ('[[ $EUID == 0 ]]', 'true')):
            self.assertIn(old, script)
            script = script.replace(old, new)
        (t / 'install.sh').write_text(script)
        self.t, self.record = t, t / 'ws/.devcontainer/installed.local.txt'

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, *args):
        env = dict(os.environ, FAKE=str(self.t), PATH=f'{self.t}/bin:{os.environ["PATH"]}',
                   ROS_WS=str(self.t / 'ws'), ROS_DISTRO='jazzy')
        env.pop('SUDO_UID', None)
        return subprocess.run(['bash', str(self.t / 'install.sh'), *args], env=env,
                              capture_output=True, text=True)

    def test_records_new_installs_once_with_a_promotion_hint(self):
        out = self.run_install('nav2_bringup', 'rclpy', 'pip:pyubx2>=1.3.9')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('✓ nav2_bringup · deb ros-jazzy-nav2-bringup', out.stdout)
        self.assertIn('✓ rclpy · installed', out.stdout)
        self.assertIn('Keep it: add "nav2_bringup" to .devcontainer/packages.txt', out.stdout)
        self.assertIn('Keep it: add "pyubx2>=1.3.9" to .devcontainer/requirements.txt', out.stdout)
        first = [line.split()[0] for line in self.record.read_text().splitlines()]
        self.assertEqual(first, ['pip:pyubx2>=1.3.9', 'nav2_bringup'])
        self.run_install('nav2_bringup')
        self.assertEqual(len(self.record.read_text().splitlines()), 2)

    def test_image_lists_are_not_recorded_and_bad_specs_refused(self):
        listing = self.t / 'list.txt'
        listing.write_text('htop  # comment\n')
        self.assertEqual(self.run_install('--file', str(listing)).returncode, 0)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.run_install('pip:x;rm').returncode, 2)


if __name__ == '__main__':
    unittest.main()
