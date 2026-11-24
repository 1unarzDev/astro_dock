"""astro's command surface: help levels, host forwarding of help.yaml entries, pickers (choices_from), status
with the stacks, rec fetch and clock. No network: subprocess, terminals and the vessel are stubbed."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
os.environ['ASTRO_FIELD'] = 'off'

from astro import cli, clock, config, rec, remote, stacks, status  # noqa: E402

HELP = cli.help_module()
HOSTS = {'boat-main': {'user': 'crew', 'address': '10.0.0.2', 'ssh': True, 'container': '', 'workspace': '',
                       'via': '', 'roles': ['computer']},
         'station': {'user': 'op', 'address': '10.0.0.9', 'ssh': True, 'container': '', 'workspace': '',
                     'via': '', 'roles': ['operator']}}


class Vessel:
    name, registry, hosts_kind = 'boat', 'devices.yaml', {}
    astro = {'home': 'computer'}

    def hosts(self):
        return HOSTS

    def data_root(self):
        return Path('/tmp/data')


@contextlib.contextmanager
def only(pkg):
    """dispatch sees only PKG's commands."""
    with mock.patch.object(cli, 'help_module', return_value=HELP), \
            mock.patch.object(HELP, 'discover', return_value=([pkg], [])):
        yield


def package(*commands):
    path = Path(tempfile.mkdtemp(), 'help.yaml')
    path.write_text(HELP.yaml.safe_dump({'package': 'p', 'commands': list(commands)}))
    return HELP.parse(path)


class Levels(unittest.TestCase):
    def test_core_by_default_all_adds_setup_and_a_search_sees_both(self):
        pkg = package({'name': 'drive', 'summary': 'go'}, {'name': 'overlay', 'level': 'setup', 'summary': 'build'})
        self.assertIn('drive', HELP.text([pkg]))
        self.assertNotIn('overlay', HELP.text([pkg]))
        self.assertIn('overlay', HELP.text([pkg], levels=HELP.LEVELS))
        self.assertIn('overlay', HELP.text([pkg], 'overlay'))
        with self.assertRaisesRegex(ValueError, 'level'):
            package({'name': 'x', 'level': 'expert'})

    def test_astro_help_lists_no_setup_command_and_no_removed_one(self):
        out = HELP.text(HELP.discover([HELP.OWN])[0])
        for gone in ('ps ', 'colcon build', 'whoami', 'fetch_bags', 'astro-clock', 'overlay', 'station take'):
            self.assertNotIn('  ' + gone, out)
        for kept in ('status', 'rec fetch', 'clock', 'profile'):
            self.assertIn('  ' + kept, out)


class HostForwarding(unittest.TestCase):
    ENTRY = {'name': 'thrusters free', 'run': 'ros2 run hw free', 'host': 'computer', 'params': []}

    def test_decision(self):
        vessel = Vessel()
        self.assertEqual(cli.entry_host(vessel, self.ENTRY, False, 'station', False)[0], 'boat-main')
        self.assertEqual(cli.entry_host(vessel, self.ENTRY, False, 'boat-main', False)[0], 'boat-main')  # its host shell
        self.assertIsNone(cli.entry_host(vessel, self.ENTRY, False, 'boat-main', True))   # in its container
        self.assertIsNone(cli.entry_host(vessel, self.ENTRY, True, 'station', False))     # --here
        self.assertIsNone(cli.entry_host(vessel, dict(self.ENTRY, host=''), False, 'station', False))

    def test_dispatch_forwards_over_ssh_with_the_args(self):
        pkg = package(dict(self.ENTRY, params=[{'name': '--check', 'type': 'bool'}]))
        with only(pkg), mock.patch.object(config, 'Vessel', return_value=Vessel()), \
                mock.patch.object(config, 'this_host', return_value='station'), \
                mock.patch.object(remote, 'in_container', return_value=False), \
                mock.patch('subprocess.call', return_value=0) as call, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.dispatch(['thrusters', 'free', '--check']), 0)
        argv = call.call_args[0][0]
        self.assertEqual(argv[:4], ['ssh', '-o', 'ConnectTimeout=8', 'crew@10.0.0.2'])
        self.assertIn("ros2 run hw free --check", argv[-1])
        self.assertIn('(on boat-main)', err.getvalue())

    def test_a_noun_alone_lists_its_subcommands(self):
        pkg = package({'name': 'thrusters test', 'summary': 'pulse'}, {'name': 'thrusters free', 'summary': 'free'})
        with only(pkg), mock.patch('subprocess.call') as call, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.dispatch(['thrusters']), 0)
        call.assert_not_called()
        self.assertIn('astro thrusters test', out.getvalue())
        self.assertIn('astro thrusters free', out.getvalue())


class Choices(unittest.TestCase):
    def test_parse_lines_with_and_without_a_description(self):
        self.assertEqual(HELP.parse_choices('a\tfirst one\nb\n\n  \nc\t\n'), [('a', 'first one'), ('b', ''), ('c', '')])
        self.assertEqual(HELP.run_choices("printf 'x\\ty\\nz\\n'"), [('x', 'y'), ('z', '')])

    def test_without_a_terminal_the_choices_are_printed_and_dispatch_exits_2(self):
        pkg = package({'name': 'device', 'run': 'echo ran', 'params': [
            {'name': 'part', 'choices_from': "printf 'camera\\tthe zed\\nlidar\\n'"}]})
        with only(pkg), mock.patch('subprocess.call') as call, \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.dispatch(['device']), 2)
        call.assert_not_called()
        self.assertIn('camera', out.getvalue())
        self.assertIn('the zed', out.getvalue())

    def test_esc_in_the_picker_runs_nothing_and_exits_1_with_one_line(self):
        pkg = package({'name': 'device', 'run': 'echo ran', 'params': [
            {'name': 'part', 'description': 'The part.', 'choices_from': "printf 'camera\\tthe zed\\n'"}]})
        seen = {}

        def pick(title, options, current=None):
            seen['title'] = title
            return None                                   # Esc

        with only(pkg), mock.patch.object(HELP, 'pick', pick), mock.patch('subprocess.call') as call, \
                mock.patch('sys.stdin.isatty', return_value=True), mock.patch('sys.stdout.isatty', return_value=True), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.dispatch(['device']), 1)
        call.assert_not_called()
        self.assertEqual(seen['title'], 'Pick the part for astro device (The part.):')
        self.assertEqual(err.getvalue(), 'astro device: cancelled, nothing run\n')

    def test_a_given_value_needs_no_picker_and_a_picked_one_is_appended(self):
        entry = package({'name': 'device', 'params': [{'name': 'part', 'choices_from': 'echo camera'},
                                                      {'name': '--fast', 'type': 'bool'}]})['commands'][0]
        self.assertEqual(cli.fill(HELP, entry, ['lidar']), ['lidar'])
        self.assertEqual(cli.fill(HELP, entry, ['--fast'], pick=lambda title, options, current=None: options[0][0]),
                         ['--fast', 'camera'])


class StatusHasTheStacks(unittest.TestCase):
    def test_table_and_page(self):
        rows = [('drive', 'up', 'boat-main', '0:05:00', '/odom 20Hz', '/d/drive.log'),
                ('nav', 'down*', 'boat-main', '', '', '')]
        lines = stacks.table(rows)
        self.assertIn('* started outside astro', lines[-1])
        with mock.patch.object(config, 'have_ros', return_value=False):
            page = status.page(type('V', (), {'name': 'boat', 'data_root': lambda self: Path('/nonexistent')})(),
                               None, 'astro test still', stack_lines=lines)
        self.assertRegex(page.splitlines()[1], r'^stacks drive\s+up\s+boat-main\s+0:05:00\s+/odom 20Hz\s+/d/drive.log$')
        self.assertIn('next   astro test still', page)

    def test_forwarded_status_shows_the_stacks_from_here_first(self):
        vessel = type('V', (Vessel,), {'astro': {'home': 'computer', 'stacks': {'drive': {}}}})()
        with mock.patch.object(config, 'have_ros', return_value=False), \
                mock.patch.object(cli.Session, 'vessel', new=vessel), \
                mock.patch.object(stacks, 'rows', return_value=[('drive', 'up', 'boat-main', '', '', '')]), \
                mock.patch.object(cli, 'forward', return_value=0) as fwd, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['status']), 0)
        self.assertTrue(out.getvalue().startswith('stacks drive'))
        self.assertEqual(fwd.call_args[1]['env'], {'ASTRO_NO_STACKS': '1'})


class RecFetch(unittest.TestCase):
    def test_bags_parsed_newest_first_and_found(self):
        lines = ['astro-bag\t2026-11-23\t101500-lake\t/d/2026-11-23/bags/101500-lake\t2048\t1000\t1300',
                 'astro-bag\t2026-11-24\t090000\t/d/2026-11-24/bags/090000\t5\t-\t-', 'noise']
        bags = rec.parse_bags(lines)
        self.assertEqual([b['name'] for b in bags], ['090000', '101500-lake'])
        value, about = rec.choice(bags[1])
        self.assertEqual(value, '2026-11-23/101500-lake')
        self.assertIn('lake', about)
        self.assertIn('2.0 KB', about)
        self.assertEqual(rec.find(bags, 'lake')['date'], '2026-11-23')
        with self.assertRaisesRegex(config.Error, 'matches 2'):
            rec.find(bags, '0')

    def test_rsync_argv_goes_through_astro_rsh_into_the_data_dir(self):
        argv = rec.fetch_argv('boat-main', '/d/2026-11-23/bags/101500-lake', Path('/here/2026-11-23/bags/101500-lake'),
                              rec.rsh())
        self.assertEqual(argv[0], 'rsync')
        self.assertEqual(argv[-2:], ['boat-main:/d/2026-11-23/bags/101500-lake/', '/here/2026-11-23/bags/101500-lake/'])
        self.assertTrue(argv[argv.index('-e') + 1].endswith('tools/bin/astro _rsh'))
        self.assertIn('--partial', argv)

    def test_rsh_runs_rsyncs_server_in_the_hosts_container(self):
        with mock.patch('os.execvp') as execvp:
            rec.rsh_main(Vessel(), ['-l', 'crew', 'boat-main', 'rsync', '--server', '--sender', '.', '/d/x/'])
        argv = execvp.call_args[0][1]
        self.assertEqual(argv[3], 'crew@10.0.0.2')
        self.assertIn("rsync --server --sender . /d/x/", argv[-1])
        self.assertIn('docker exec -i', argv[-1])


class Clock(unittest.TestCase):
    def test_offset_and_sources(self):
        lines = ['astro-clock 1700000000.500', 'astro-chrony',
                 'astro-src ^,?,10.0.0.9,0,6,0,-,0.0,0.0,0.0', 'astro-src ^,*,10.0.0.1,2,6,377,12,0,0,0']
        found = clock.parse(lines, {0: 1700000000.0})
        self.assertAlmostEqual(found['offset'], 0.5)
        self.assertEqual(clock.source_text(found['sources']), 'synced to 10.0.0.1')
        self.assertEqual(clock.source_text(found['sources'][:1]), 'NOT synced: 10.0.0.9 unreachable')
        self.assertEqual(clock.source_text([], chrony=False), 'no chronyc')
        self.assertEqual(clock.offset_text(-342 * 86400 - 3 * 3600 - 5), '-342 d 3 h')
        self.assertEqual(clock.offset_text(0.012), '+12 ms')

    def test_hosts_leave_this_machine_out_and_fix_only_robots(self):
        names = [n for n, _ in clock.hosts(Vessel(), me='station')]
        self.assertEqual(names, ['boat-main'])
        self.assertFalse(clock.robot(HOSTS['station']))
        argv = remote.host_shell(Vessel(), 'boat-main', HOSTS['boat-main'], clock.SET)
        self.assertNotIn('docker', ' '.join(argv))
        self.assertIn('sudo -n date -u -s', argv[-1])


if __name__ == '__main__':
    unittest.main()
