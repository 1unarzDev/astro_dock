"""astro's forwarding: outside ROS, the verbs that need ROS or the vessel's data run on the profile's astro.home
(`astro on HOME astro ...`). No network: subprocess and the vessel are stubbed."""
import datetime
import io
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
os.environ['ASTRO_FIELD'] = 'off'

from astro import cli, config, remote  # noqa: E402
from astro.engine import log  # noqa: E402


class FakeVessel:
    name = 'boat'
    registry = 'devices.yaml'
    hosts_kind = {}

    def __init__(self, home='computer', ssh=True):
        self.astro = {'home': home} if home else {}
        self.ssh = ssh

    def hosts(self):
        return {'boat-main': {'user': 'crew', 'address': '10.0.0.2', 'ssh': self.ssh, 'container': 'dev1',
                              'workspace': '', 'via': '', 'roles': ['computer']},
                'boat-cam': {'user': 'crew', 'address': '10.0.0.3', 'ssh': True, 'container': '',
                             'workspace': '', 'via': '', 'roles': ['camera']}}

    def env(self):
        return {'ASTRO_VESSEL': self.name}

    def state(self):
        return Path('/tmp/state.yaml')

    def data_root(self):
        return Path('/tmp/data')


class Decision(unittest.TestCase):
    def test_home_verbs_forward_outside_ros(self):
        for verb in ('status', 'rec', 'mark', 'log', 'test', 'apply', 'undo', 'bundle'):
            self.assertTrue(cli.forwards(verb, False, False, 'computer'), verb)

    def test_local_verbs_never_forward(self):
        for verb in ('help', 'docs', 'on', 'up', 'down', 'ps', 'logs', 'sim', 'check', 'issue', 'sync', 'show',
                     'whoami', 'env', 'hot'):
            self.assertFalse(cli.forwards(verb, False, False, 'computer'), verb)

    def test_here_ros_or_no_home_stays_here(self):
        self.assertFalse(cli.forwards('test', True, False, 'computer'))
        self.assertFalse(cli.forwards('test', False, True, 'computer'))
        self.assertFalse(cli.forwards('test', False, False, None))
        self.assertFalse(cli.forwards('test', False, False, ''))


class Main(unittest.TestCase):
    def run_main(self, argv, vessel, ros=False, env=None):
        with mock.patch.object(config, 'have_ros', return_value=ros), \
                mock.patch.object(cli.Session, 'vessel', new=vessel), \
                mock.patch.object(cli, 'forward', return_value=0) as fwd, \
                mock.patch.dict(os.environ, env or {}), \
                mock.patch('sys.stdout', new=io.StringIO()), mock.patch('sys.stderr', new=io.StringIO()) as err:
            code = cli.main(argv)
        return code, fwd, err.getvalue()

    def test_forwards_the_same_args(self):
        code, fwd, _ = self.run_main(['rec', 'start', 'lean', 'lake'], FakeVessel())
        self.assertEqual(code, 0)
        fwd.assert_called_once()
        self.assertEqual(fwd.call_args[0][1:], ('computer', ['rec', 'start', 'lean', 'lake']))

    def test_here_flag_is_stripped_and_runs_here(self):
        with mock.patch.object(cli, 'cmd_log', return_value=0) as local:
            code, fwd, _ = self.run_main(['log', '--here', '-n', '3'], FakeVessel())
        fwd.assert_not_called()
        self.assertEqual(local.call_args[0][1].n, 3)
        self.assertEqual(code, 0)

    def test_astro_here_env_runs_here(self):
        with mock.patch.object(cli, 'cmd_log', return_value=0) as local:
            self.run_main(['log'], FakeVessel(), env={'ASTRO_HERE': '1'})
        local.assert_called_once()

    def test_with_ros_runs_here(self):
        with mock.patch.object(cli, 'cmd_log', return_value=0) as local:
            _, fwd, _ = self.run_main(['log'], FakeVessel(), ros=True)
        fwd.assert_not_called()
        local.assert_called_once()

    def test_home_missing_keeps_the_needs_ros_error(self):
        code, fwd, err = self.run_main(['rec', 'start'], FakeVessel(home=None))
        fwd.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn('needs ROS', err)
        self.assertIn('astro.home', err)

    def test_local_verb_with_home_does_not_forward(self):
        with mock.patch.object(cli.field, 'show', return_value=0) as show:
            _, fwd, _ = self.run_main(['show'], FakeVessel())
        fwd.assert_not_called()
        show.assert_called_once()


class Argv(unittest.TestCase):
    def test_ssh_home_runs_astro_in_its_container(self):
        host, argv, env = remote.home_words(FakeVessel(), 'computer', ['mark', 'wind', "it's up"], False)
        self.assertEqual(host, 'boat-main')
        self.assertIsNone(env)
        self.assertEqual(argv[:2], ['ssh', 'crew@10.0.0.2'])
        self.assertNotIn('-t', argv)
        inner = shlex.split(argv[-1])
        self.assertEqual(inner[:3], ['docker', 'exec', '-i'])
        self.assertEqual(inner[-3:-1], ['bash', '-c'])
        self.assertEqual(shlex.split(inner[-1]), ['ASTRO_HERE=1', 'astro', 'mark', 'wind', "it's up"])

    def test_tty_and_host_name(self):
        host, argv, _ = remote.home_words(FakeVessel(), 'boat-main', ['test', 'still'], True)
        self.assertEqual(host, 'boat-main')
        self.assertIn('-t', argv)
        self.assertIn('docker exec -it', argv[-1])

    def test_local_home_execs_into_its_container(self):
        _, argv, _ = remote.home_words(FakeVessel(ssh=False), 'computer', ['status'], False)
        self.assertEqual(argv[:3], ['docker', 'exec', '-i'])
        self.assertIn('ASTRO_VESSEL=boat', argv)
        self.assertEqual(argv[-4:], ['dev1', 'bash', '-c', 'ASTRO_HERE=1 astro status'])

    def test_unknown_home_is_an_error(self):
        with self.assertRaises(config.Error):
            remote.home_words(FakeVessel(), 'nowhere', ['status'], False)


class Forward(unittest.TestCase):
    def call(self, args, tty=False, code=0):
        with mock.patch.object(remote, 'home_words', return_value=('boat-main', ['ssh', 'x'], None)) as words, \
                mock.patch.object(cli.subprocess, 'Popen') as popen, \
                mock.patch.object(cli.subprocess, 'call', return_value=code) as call, \
                mock.patch.object(remote, 'replay_marks', return_value=(0, 0)) as replay, \
                mock.patch('sys.stdout', new=io.StringIO()) as out, mock.patch('sys.stderr', new=io.StringIO()) as err:
            result = cli.forward(FakeVessel(), 'computer', args, tty)
        self.replay = replay
        return result, words, popen, call, out.getvalue(), err.getvalue()

    def test_mark_is_backgrounded_with_its_time_and_no_tty(self):
        code, words, popen, call, _, err = self.call(['mark', 'wind', 'up'], tty=True)
        self.assertEqual(code, 0)
        call.assert_not_called()
        words.assert_not_called()
        argv = popen.call_args[0][0]
        self.assertEqual(argv[1:3], ['-c', cli.DELIVER])
        self.assertEqual(argv[4], 'computer')
        self.assertRegex(argv[5], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$')
        self.assertEqual(argv[6:], ['wind', 'up'])
        self.assertTrue(popen.call_args[1]['start_new_session'])
        self.assertIn('(on boat-main)', err)

    def test_test_run_prints_the_link_reminder(self):
        _, words, _, call, out, _ = self.call(['test', 'still'], tty=True)
        call.assert_called_once()
        self.assertTrue(words.call_args[0][3])
        self.assertIn('running on boat-main; if the link drops the test continues', out)

    def test_test_list_has_no_reminder(self):
        _, _, _, _, out, _ = self.call(['test', 'list'])
        self.assertNotIn('link drops', out)

    def test_pending_marks_go_after_a_successful_command_only(self):
        self.call(['log'])
        self.replay.assert_called_once()
        self.call(['log'], code=255)
        self.replay.assert_not_called()


class PendingMarks(unittest.TestCase):
    """A forwarded mark whose ssh fails is kept locally and sent later with its original time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {'XDG_STATE_HOME': self.tmp.name})
        self.env.start()
        self.vessel = FakeVessel()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_with(self, codes, fn, *args):
        codes = list(codes)
        sent = []

        def fake_run(argv, **kwargs):
            sent.append(shlex.split(shlex.split(argv[-1])[-1]))
            self.assertEqual(kwargs['timeout'], remote.MARK_TIMEOUT)
            return mock.Mock(returncode=codes.pop(0))
        with mock.patch.object(remote.subprocess, 'run', side_effect=fake_run), \
                mock.patch('sys.stderr', new=io.StringIO()) as err:
            result = fn(*args)
        return result, sent, err.getvalue()

    def pending(self):
        path = remote.pending_path(self.vessel)
        return path.read_text().splitlines() if path.exists() else None

    def test_failed_mark_is_kept_with_one_line(self):
        at = '2026-11-24T14:02:03-06:00'
        code, sent, err = self.run_with([255], remote.deliver_mark, self.vessel, 'computer', at, ['wind', "it's up"])
        self.assertEqual(code, 1)
        self.assertEqual(sent, [['ASTRO_HERE=1', 'astro', 'mark', '--at', at, 'wind', "it's up"]])
        self.assertEqual(self.pending(), [f"{at}\twind it's up"])
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn('not reachable; kept in', err)

    def test_replay_keeps_order_and_times_and_stops_at_a_failure(self):
        for at, text in (('2026-11-24T14:00:00-06:00', 'one'), ('2026-11-24T14:01:00-06:00', 'two words'),
                         ('2026-11-24T14:02:00-06:00', 'three')):
            remote.queue_mark(self.vessel, at, text)
        (sent, left), argvs, err = self.run_with([0, 255], remote.replay_marks, self.vessel, 'computer')
        self.assertEqual((sent, left), (1, 2))
        self.assertEqual(argvs[0][2:], ['mark', '--at', '2026-11-24T14:00:00-06:00', 'one'])
        self.assertEqual(argvs[1][2:], ['mark', '--at', '2026-11-24T14:01:00-06:00', 'two', 'words'])
        self.assertEqual(self.pending(), ['2026-11-24T14:01:00-06:00\ttwo words', '2026-11-24T14:02:00-06:00\tthree'])
        self.assertIn('2 still pending', err)
        (sent, left), _, _ = self.run_with([0, 0], remote.replay_marks, self.vessel, 'computer')
        self.assertEqual((sent, left), (2, 0))
        self.assertIsNone(self.pending())

    def test_a_delivered_mark_sends_the_pending_ones(self):
        remote.queue_mark(self.vessel, '2026-11-24T13:00:00-06:00', 'old')
        code, sent, _ = self.run_with([0, 0], remote.deliver_mark, self.vessel, 'computer',
                                      '2026-11-24T14:00:00-06:00', ['new'])
        self.assertEqual(code, 0)
        self.assertEqual([s[-1] for s in sent], ['new', 'old'])
        self.assertIsNone(self.pending())

    def test_nothing_pending_runs_nothing(self):
        (sent, left), argvs, _ = self.run_with([], remote.replay_marks, self.vessel, 'computer')
        self.assertEqual(((sent, left), argvs), ((0, 0), []))

    def test_mark_at_writes_the_original_time_and_day(self):
        when = remote.mark_time('2026-11-23T23:59:58')
        root = Path(self.tmp.name) / 'data'
        with mock.patch('sys.stdout', new=io.StringIO()):
            log.write(root, 'mark', 'late', when=when)
        self.assertEqual((root / '2026-11-23/log.txt').read_text(), '23:59:58  mark    late\n')
        self.assertEqual(remote.mark_time('2026-11-24T20:00:00+00:00'),
                         datetime.datetime(2026, 11, 24, 20, tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None))


class FieldHome(unittest.TestCase):
    def test_sync_and_show_fetch_only_from_an_ssh_home(self):
        with mock.patch.object(config, 'have_ros', return_value=False):
            self.assertEqual(cli.field_home(FakeVessel(), False), 'computer')
            self.assertIsNone(cli.field_home(FakeVessel(ssh=False), False))     # this machine's container
            self.assertIsNone(cli.field_home(FakeVessel(), True))
            self.assertIsNone(cli.field_home(FakeVessel(home=None), False))
        with mock.patch.object(config, 'have_ros', return_value=True):
            self.assertIsNone(cli.field_home(FakeVessel(), False))


if __name__ == '__main__':
    unittest.main()
