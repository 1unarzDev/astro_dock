"""Small filesystem regressions for the installed resource gate (no ROS)."""
from pathlib import Path
import tempfile
import unittest

from check_config_install import check_resources


class ResourceGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = self.root / 'install'
        self.name = 'mhseals_nav'
        self.origin = self.prefix / 'lib/mhseals_nav/__init__.py'
        self.share = self.prefix / 'share' / self.name
        self.marker = self.prefix / 'share/ament_index/resource_index/packages' / self.name
        for path in [self.origin, self.marker, self.share / 'package.xml',
                     self.share / 'config/nav2_params.yaml',
                     self.share / 'launch/robot.launch.py',
                     self.share / 'behavior_trees/tree.xml']:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.external = self.root / 'external'
        self.external.write_text('external')

    def check(self):
        return check_resources(self.prefix, self.name, self.origin)

    def test_valid_resources(self):
        self.assertTrue(self.check())

    def test_backup_is_not_required_file(self):
        for suffix in ['config/nav2_params.yaml', 'launch/robot.launch.py', 'package.xml']:
            with self.subTest(suffix=suffix):
                path = self.share / suffix
                backup = path.with_name(path.name + '.bak')
                path.rename(backup)
                with self.assertRaises(SystemExit):
                    self.check()
                backup.rename(path)

    def test_external_marker_and_share_file(self):
        for path in [self.marker, self.share / 'config/nav2_params.yaml']:
            with self.subTest(path=path):
                path.unlink()
                path.symlink_to(self.external)
                with self.assertRaises(SystemExit):
                    self.check()
                path.unlink()
                path.touch()

    def test_external_hardware_default(self):
        self.name = 'mhseals_hardware'
        share = self.prefix / 'share' / self.name
        share.mkdir()
        (share / 'package.xml').touch()
        (self.marker.parent / self.name).touch()
        default = self.origin.parent / 'config/default.yaml'
        default.parent.mkdir()
        default.symlink_to(self.external)
        with self.assertRaises(SystemExit):
            self.check()

    def test_external_resource_directory(self):
        directory = self.share / 'behavior_trees'
        (directory / 'tree.xml').unlink()
        directory.rmdir()
        external_dir = self.root / 'trees'
        external_dir.mkdir()
        (external_dir / 'tree.xml').touch()
        directory.symlink_to(external_dir, target_is_directory=True)
        with self.assertRaises(SystemExit):
            self.check()


if __name__ == '__main__':
    unittest.main()
