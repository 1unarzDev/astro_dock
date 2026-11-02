"""install-check.py: a copied install/ that src/ has moved past warns; a current copy or a symlink-install
does not; colcon_defaults.yaml makes every workspace build a symlink-install."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / '.devcontainer/install-check.py'


class InstallCheck(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ws)
        self.src = self.ws / 'src/repo/pkg/mod'
        (self.src / 'sub').mkdir(parents=True)
        (self.src.parent / 'setup.py').write_text('')
        (self.src / '__init__.py').write_text('')
        (self.src / 'sub/cli.py').write_text('VERSION = 1\n')
        self.site = self.ws / 'install/lib/python3.12/site-packages'
        self.site.mkdir(parents=True)

    def check(self):
        return subprocess.run([sys.executable, '-I', str(CHECK), str(self.ws)], capture_output=True, text=True)

    def test_a_copy_src_moved_past_warns(self):
        shutil.copytree(self.src, self.site / 'mod')
        self.assertEqual(self.check().returncode, 0)
        (self.src / 'sub/cli.py').write_text('VERSION = 2\n')
        (self.src / 'new.py').write_text('')
        run = self.check()
        self.assertEqual(run.returncode, 1)
        self.assertIn('mod (2 files)', run.stderr)
        self.assertIn('--symlink-install', run.stderr)

    def test_symlink_install_and_no_install_are_quiet(self):
        self.assertEqual(self.check().stderr, '')
        (self.site / 'mod').symlink_to(self.src)
        (self.src / 'sub/cli.py').write_text('VERSION = 2\n')
        run = self.check()
        self.assertEqual((run.returncode, run.stderr), (0, ''))

    def test_workspace_builds_symlink(self):
        build = yaml.safe_load((ROOT / 'colcon_defaults.yaml').read_text())['build']
        self.assertIs(build.get('symlink-install'), True)


if __name__ == '__main__':
    unittest.main()
