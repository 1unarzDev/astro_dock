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

    def docker(self, state):
        """config.dev_container as docker would answer with the container STATE (running, stopped or none);
        docker start and a create make it run."""
        self.state = state

        def lookup(root=None, stopped=False):
            return 'abc123' if self.state == 'running' or (stopped and self.state == 'stopped') else ''

        def run(argv):
            self.ran.append(argv)
            if argv[:2] == ['docker', 'start'] or argv[-1] == 'dev' or argv[:2] == ['devcontainer', 'up']:
                self.state = 'running'
            return 0
        return mock.patch.object(config, 'dev_container', side_effect=lookup), run

    def test_a_stopped_container_is_started_as_it_is_never_rebuilt(self):
        patch, run = self.docker('stopped')
        with patch, mock.patch.object(remote.shutil, 'which', return_value='/usr/bin/devcontainer'):
            remote.start(self.root, run)
        assert self.ran == [['docker', 'start', 'abc123']]            # no prebuild, no devcontainer up
        assert remote.os.execvp.call_args[0][1][-2:] == ['abc123', 'bash']

    def test_no_container_is_set_up_once_created_then_opened(self):
        patch, run = self.docker('none')
        with patch, mock.patch.object(remote.shutil, 'which', return_value='/usr/bin/devcontainer'):
            remote.start(self.root, run)
        assert self.ran == [[str(self.root / '.devcontainer' / 'prebuild.sh')],
                            ['devcontainer', 'up', '--workspace-folder', str(self.root)]]
        assert remote.os.execvp.call_args[0][1][-2:] == ['abc123', 'bash']
        # set up already (the override exists) and no devcontainer CLI: compose creates it, never recreating one
        (self.root / '.devcontainer' / 'docker-compose.override.yml').write_text('')
        self.ran.clear()
        patch, run = self.docker('none')
        with patch, mock.patch.object(remote.shutil, 'which', return_value=None):
            remote.start(self.root, run)
        assert len(self.ran) == 1 and self.ran[0][:2] == ['docker', 'compose']
        assert self.ran[0][-4:] == ['up', '-d', '--no-recreate', 'dev']

    def test_a_failed_start_stops_and_inside_a_container_nothing_runs(self):
        with mock.patch.object(config, 'dev_container', return_value=''), \
                mock.patch.object(remote.shutil, 'which', return_value=None):
            (self.root / '.devcontainer' / 'docker-compose.override.yml').write_text('')
            assert remote.start(self.root, lambda argv: 3) == 3
        with mock.patch.object(config, 'dev_container', side_effect=lambda root=None, stopped=False:
                               'abc123' if stopped else ''):
            assert remote.start(self.root, lambda argv: 5) == 5      # a stopped one that will not start
        remote.os.execvp.assert_not_called()
        with mock.patch.object(remote, 'in_container', return_value=True):
            assert remote.start(self.root, self.run_) == 0 and self.ran == []


if __name__ == '__main__':
    unittest.main()


class DevContainer(unittest.TestCase):
    """config.dev_container finds this checkout's container by name, else by its Compose labels (one made
    under an older default name), and never another checkout's."""

    def lookup(self, running, stopped=False):
        """dev_container of a checkout whose docker lists RUNNING ({id: (name, working_dir[, state])})."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, 'ws')
            (root / '.devcontainer').mkdir(parents=True)
            (root / 'devices').symlink_to(ROOT / 'devices')
            (root / '.devcontainer/.env').write_text('ASTRO_CONTAINER=astro_dev\n')
            docker = Path(tmp, 'docker')
            rows = ''.join(f'{cid} {row[0]} {row[1].replace("ROOT", str(root.resolve()))} '
                           f'{row[2] if len(row) > 2 else "running"}\n' for cid, row in running.items())
            docker.write_text(f'''#!/bin/bash
name=; wd=; service=; all=
for a in "$@"; do case $a in -aq) all=1;; name=*) name=${{a#name=^}}; name=${{name%$}};;
  label=com.docker.compose.project.working_dir=*) wd=${{a#*=}}; wd=${{wd#*=}};;
  label=com.docker.compose.service=dev) service=dev;; esac; done
while read -r id n w s; do
  [[ $s == running || -n $all ]] || continue
  if [[ -n $name && $n == "$name" ]] || [[ -n $wd && $service == dev && $w == "$wd" ]]; then echo "$id"; fi
done <<'ROWS'
{rows}ROWS
''')
            docker.chmod(0o755)
            with mock.patch.dict(os.environ, {'HW_DOCKER': str(docker)}):
                return config.dev_container(root / 'devices' / '..', stopped=stopped)

    def test_by_name_then_by_label_never_another_checkouts(self):
        self.assertEqual(self.lookup({'aaa': ('astro_dev', '/elsewhere/.devcontainer')}), 'aaa')
        self.assertEqual(self.lookup({'bbb': ('old_dev', 'ROOT/.devcontainer')}), 'bbb')   # an older default name
        self.assertEqual(self.lookup({'ccc': ('old_dev', '/other/ws/.devcontainer')}), '')

    def test_all_also_finds_a_stopped_one(self):
        self.assertEqual(self.lookup({'ddd': ('astro_dev', '/elsewhere/.devcontainer', 'exited')}), '')
        self.assertEqual(self.lookup({'ddd': ('astro_dev', '/elsewhere/.devcontainer', 'exited')}, stopped=True), 'ddd')
