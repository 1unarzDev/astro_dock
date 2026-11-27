"""astro start: a running dev container opens at once; a down one is set up (prebuild once), started, then
opened; inside a container nothing runs. Docker, devcontainer and exec are stubbed."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
os.environ['ASTRO_FIELD'] = 'off'

from astro import config, remote  # noqa: E402


class Start(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / '.devcontainer').mkdir()
        patches = [mock.patch.object(remote, 'in_container', return_value=False),
                   mock.patch.object(remote, '_tty', return_value=True),
                   mock.patch.object(config, 'Vessel', side_effect=config.Error('no profile')),
                   mock.patch.object(remote.os, 'execvp')]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.ran = []

    def run_(self, argv):
        self.ran.append(argv)
        return 0

    def test_a_running_container_opens_without_starting_anything(self):
        with mock.patch.object(config, 'dev_container', return_value='abc123'):
            remote.start(self.root, self.run_)
        assert self.ran == []
        remote.os.execvp.assert_called_once_with('docker', ['docker', 'exec', '-it', 'abc123', 'bash'])

    def test_a_down_container_is_set_up_once_started_then_opened(self):
        with mock.patch.object(config, 'dev_container', side_effect=['', 'abc123']), \
                mock.patch.object(remote.shutil, 'which', return_value='/usr/bin/devcontainer'):
            remote.start(self.root, self.run_)
        assert self.ran == [[str(self.root / '.devcontainer' / 'prebuild.sh')],
                            ['devcontainer', 'up', '--workspace-folder', str(self.root)]]
        assert remote.os.execvp.call_args[0][1][-2:] == ['abc123', 'bash']
        # set up already (the override exists) and no devcontainer CLI: compose starts it, no prebuild
        (self.root / '.devcontainer' / 'docker-compose.override.yml').write_text('')
        self.ran.clear()
        with mock.patch.object(config, 'dev_container', side_effect=['', 'abc123']), \
                mock.patch.object(remote.shutil, 'which', return_value=None):
            remote.start(self.root, self.run_)
        assert len(self.ran) == 1 and self.ran[0][:2] == ['docker', 'compose'] and self.ran[0][-3:] == ['up', '-d', 'dev']

    def test_a_failed_start_stops_and_inside_a_container_nothing_runs(self):
        with mock.patch.object(config, 'dev_container', return_value=''), \
                mock.patch.object(remote.shutil, 'which', return_value=None):
            (self.root / '.devcontainer' / 'docker-compose.override.yml').write_text('')
            assert remote.start(self.root, lambda argv: 3) == 3
        remote.os.execvp.assert_not_called()
        with mock.patch.object(remote, 'in_container', return_value=True):
            assert remote.start(self.root, self.run_) == 0 and self.ran == []


if __name__ == '__main__':
    unittest.main()
