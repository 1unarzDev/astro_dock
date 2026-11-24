import importlib.util
import itertools
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('astro_help', ROOT / '.devcontainer' / 'astro-help.py')
astro_help = importlib.util.module_from_spec(spec)
spec.loader.exec_module(astro_help)

HW_ENV = dict(os.environ, ASTRO_SECRETS='/nonexistent',
              ASTRO_PROFILES=str(ROOT / 'tests/data/profiles'), ASTRO_VESSEL='a')   # the fixture fleet
LAUNCH = {'name': 'robot.launch', 'run': 'ros2 launch pkg robot.launch.py',
          'params': [{'name': 'sim', 'type': 'bool', 'default': True}]}


def command(**fields):
    package = Path(tempfile.mkdtemp(), 'help.yaml')
    package.write_text(astro_help.yaml.safe_dump({'package': 'p', 'commands': [fields]}))
    return astro_help.parse(package)['commands'][0]


class DiscoveryTest(unittest.TestCase):
    def test_order_dedupe_and_broken_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, share = Path(tmp, 'ws'), Path(tmp, 'opt', 'share')
            files = {ws / 'src/repo/help.yaml': 'package: alpha\ncommands: [{name: from_source}]',
                     ws / 'src/repo/beta/help.yaml': 'package: beta\n',
                     ws / 'src/bad/help.yaml': 'package: [unclosed',
                     ws / 'src/repo/build/x/help.yaml': 'package: never\n',
                     share / 'alpha/help.yaml': 'package: alpha\ncommands: [{name: from_share}]',
                     share / 'gamma/help.yaml': 'commands: []'}
            for path, body in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            paths = list(astro_help.candidates(ws, [str(share.parent)]))
            packages, errors = astro_help.discover(paths)
        self.assertEqual(paths[0], astro_help.OWN)
        self.assertEqual([p['package'] for p in packages], ['astro', 'alpha', 'beta'])
        self.assertEqual(packages[1]['commands'][0]['name'], 'from_source')
        self.assertEqual(len(errors), 2)  # the unclosed list and the nameless package
        self.assertTrue(all(e.startswith('help: ') for e in errors))

    def test_malformed_shapes_are_skipped(self):
        bodies = ['package: p\ncommands: 5', 'package: p\ncommands: [{name: c, params: 5}]',
                  'package: p\ncommands: [{name: c, examples: 5}]']
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp, f'{i}.yaml') for i in range(len(bodies))]
            for path, body in zip(paths, bodies):
                path.write_text(body)
            packages, errors = astro_help.discover(paths + [astro_help.OWN])
        self.assertEqual([p['package'] for p in packages], ['astro'])
        self.assertEqual(len(errors), 3)

    def test_workspace_falls_back_like_dev_bashrc(self):
        with mock.patch.dict(astro_help.os.environ, {'ROS_WS': '/ws'}):
            self.assertEqual(astro_help.workspace(), '/ws')
        with mock.patch.dict(astro_help.os.environ):
            astro_help.os.environ.pop('ROS_WS', None)
            self.assertEqual(astro_help.workspace(), str(ROOT.resolve()))

    def test_own_help_parses(self):
        package = astro_help.parse(astro_help.OWN)
        self.assertIn('astro-ros-install', [c['name'] for c in package['commands']])


class BuilderTest(unittest.TestCase):
    def test_forms_are_inferred(self):
        forms = [p['form'] for p in command(name='c', params=[
            {'name': '--yes', 'type': 'bool'}, {'name': '--window'}, {'name': 'target'}])['params']]
        self.assertEqual(forms, ['switch', 'option', 'positional'])
        self.assertEqual(command(**LAUNCH)['params'][0]['form'], 'launch')

    def test_defaults_omitted_values_quoted(self):
        trial = command(name='check', run='boat_trial check', params=[
            {'name': '--window', 'type': 'float', 'default': 5.0},
            {'name': '--only', 'type': 'list'},
            {'name': '--lint', 'type': 'bool'},
            {'name': 'note', 'required': False}])
        self.assertEqual(astro_help.build(trial, {}), 'boat_trial check')
        self.assertEqual(astro_help.build(trial, {'--window': '5.0', '--only': 'gps lidar',
                                                  '--lint': True, 'note': "it's"}),
                         "boat_trial check --only gps lidar --lint 'it'\"'\"'s'")

    def test_launch_values_and_required_placeholders(self):
        launch = command(**LAUNCH)
        self.assertEqual(astro_help.build(launch, {'sim': False}),
                         'ros2 launch pkg robot.launch.py sim:=false')
        self.assertEqual(astro_help.build(launch, {'sim': 'True'}), 'ros2 launch pkg robot.launch.py')
        install = command(name='astro-ros-install', params=[{'name': 'names', 'type': 'list'}])
        self.assertEqual(astro_help.build(install, {}), 'astro-ros-install <names>')
        self.assertEqual(astro_help.missing(install, {}), ['names'])
        self.assertEqual(astro_help.missing(install, {'names': 'htop'}), [])
        task = command(name='task', run='ros2 launch p task.launch.py', params=[{'name': 'task', 'required': True}])
        self.assertEqual(astro_help.build(task, {}), 'ros2 launch p task.launch.py task:=<task>')

    def test_switch_defaulting_on_turns_off(self):
        check = command(name='c', params=[{'name': '--color', 'type': 'bool', 'default': True},
                                          {'name': '--lint', 'type': 'bool'}])
        self.assertEqual(astro_help.build(check, {'--color': True, '--lint': True}), 'c --lint')
        self.assertEqual(astro_help.build(check, {'--color': False}), 'c --no-color')

    def test_unbalanced_quote_is_reported(self):
        install = command(name='i', params=[{'name': 'names', 'type': 'list'}])
        with self.assertRaisesRegex(ValueError, 'unbalanced quote'):
            astro_help.build(install, {'names': "a 'b"})

    def test_list_usage_and_form_placeholders(self):
        mosfet = command(name='mosfet', params=[
            {'name': 'file', 'type': 'path'}, {'name': '--seconds', 'type': 'float'},
            {'name': 'side', 'choices': ['a', 'b']}, {'name': 'port', 'default': 'auto'},
            {'name': '--check', 'type': 'bool'}, {'name': 'names', 'type': 'list', 'required': False}])
        self.assertEqual(astro_help.synopsis(mosfet), '<file> [--seconds N] {a|b} [port] [--check] [names...]')
        self.assertEqual(astro_help.build(mosfet, {'file': 'x', 'side': 'a', 'port': 'auto'}), 'mosfet x a')
        port, seconds, file = (next(p for p in mosfet['params'] if p['name'] == n) for n in ('port', '--seconds', 'file'))
        self.assertEqual(astro_help.placeholder(port, '/dev/ttyACM0'), '(auto: /dev/ttyACM0)')
        self.assertEqual(astro_help.placeholder(port), '(auto)')
        self.assertEqual(astro_help.placeholder(seconds), '(not set)')
        self.assertEqual(astro_help.placeholder(file), '(required)')
        self.assertRegex(astro_help.text([astro_help.parse(astro_help.OWN)]), r'\n  rec fetch \[bag\] +Copy ')

    def test_text_lists_and_details(self):
        packages = [astro_help.parse(astro_help.OWN)]
        overview = astro_help.text(packages)
        self.assertIn('astro · ', overview)
        self.assertNotIn('$ ', overview)
        detail = astro_help.text(packages, 'apt')
        self.assertIn('$ astro-ros-install <names>', detail)
        self.assertNotIn('ros_env', detail)
        self.assertIn('No command matches', astro_help.text(packages, 'zzz'))


def run(*args, **env):
    return subprocess.run(['/bin/bash', *args], cwd=ROOT, env=dict(HW_ENV, **env), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60)


def dispatched():
    """The commands hw.sh's top-level case handles, besides its own help, and those it runs before it (no profiles)."""
    text = (ROOT / 'hw.sh').read_text()
    body = re.search(r'^case \$\{1:-\} in\n(.*?)^esac', text, re.M | re.S).group(1)
    arms = re.findall(r'^    ([^\s()]+)\)', body, re.M) + re.findall(r'^if \[\[ \$\{1:-\} =~ \^\(([\w|]+)\)\$', text, re.M)
    return {name for arm in arms for name in arm.split('|')} - {"''", '-h', '--help', 'help'}


class HelpCoverageTest(unittest.TestCase):
    """hw.sh's usage, each command's --help and astro-help.yaml name exactly what the scripts handle."""

    def test_hw_usage_matches_dispatch(self):
        usage = run('hw.sh', '--help')
        self.assertEqual(usage.returncode, 0, usage.stderr)
        self.assertEqual(run('hw.sh', 'help').stdout, usage.stdout)
        listed = set(re.findall(r'^ {7}hw\.sh (\S+)', usage.stdout, re.M))
        self.assertEqual(listed, dispatched())
        for name in sorted(listed):
            with self.subTest(command=name):
                own = run('hw.sh', name, '--help')
                self.assertEqual(own.returncode, 0, own.stderr)
                self.assertTrue(own.stdout.startswith('Usage: hw.sh ' + name), own.stdout)

    def test_astro_help_names_every_hw_command_and_no_other(self):
        """Every hw.sh command is an astro verb (run: $ROS_WS/hw.sh CMD); device VERB covers KIND VERB."""
        runs = [c['run'] for c in astro_help.parse(astro_help.OWN)['commands']]
        named = {r.split()[1] for r in runs if r.startswith('$ROS_WS/hw.sh ')}
        self.assertEqual(named, dispatched())
        self.assertIn('$ROS_WS/hw.sh device status', runs)


class ConventionTest(unittest.TestCase):
    """Every help.yaml in the workspace: a noun's actions are entries of their own (rec start, rec stop),
    never a param, and a noun lists all of them."""

    def test_no_subcommand_params_and_every_noun_lists_its_entries(self):
        packages, _ = astro_help.discover(list(astro_help.candidates(prefixes=[])))
        self.assertGreater(len(packages), 1)
        for package in packages:
            names = [c['name'] for c in package['commands']]
            for command in package['commands']:
                for param in command['params']:
                    self.assertNotIn(param['name'].lstrip('-'), ('command', 'verb', 'action', 'subcommand'),
                                     f'{package["package"]}: {command["name"]}: make each action an entry')
            rows, path = astro_help.tree(package['commands']), []
            for i, (cmd, depth, label) in enumerate(rows):
                path[depth:] = [label]
                prefix = ' '.join(path)
                under = [c['name'] for c, d, _ in itertools.takewhile(lambda r: r[1] > depth, rows[i + 1:]) if c]
                subs = [n for n in names if n.startswith(prefix + ' ')]
                self.assertEqual(sorted(under), sorted(subs), f'{package["package"]}: {prefix}')

    def test_every_net_script_is_listed_and_answers_help(self):
        runs = {c['run'] for c in astro_help.parse(astro_help.OWN)['commands']}
        for script in sorted((ROOT / 'net').glob('*.sh')):
            name = script.relative_to(ROOT).as_posix()
            with self.subTest(script=name):
                self.assertIn('$ROS_WS/' + name, runs)
                out = run(name, '--help', PATH='/nonexistent')   # a missing guard installs nothing
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertTrue(out.stdout.startswith('Usage: ' + name), out.stdout)


class EntryQualityTest(unittest.TestCase):
    """Every workspace help.yaml: no duplicate entries, and the core ones never leave a field to bare typing."""

    def packages(self):
        packages, _ = astro_help.discover(list(astro_help.candidates(prefixes=[])))
        self.assertGreater(len(packages), 1)
        return packages

    def test_no_two_entries_of_a_level_run_the_same_command(self):
        seen = {}
        for package in self.packages():
            for command in package['commands']:
                key = (command['level'], command['run'])
                self.assertNotIn(key, seen, f'{package["package"]}: {command["name"]} duplicates {seen.get(key)}')
                seen[key] = f'{package["package"]}: {command["name"]}'

    def test_core_params_say_what_they_are_and_free_text_says_why(self):
        for package in self.packages():
            for command in (c for c in package['commands'] if c['level'] == 'core'):
                for param in command['params']:
                    where = f'{package["package"]}: {command["name"]} {param["name"]}'
                    text = str(param.get('description') or '')
                    picked = param.get('choices') or param.get('choices_from')
                    self.assertGreaterEqual(len(text.split()), 8, f'{where}: say what it is ({text!r})')
                    if param['type'] != 'bool' and not picked:
                        self.assertIn('e.g.', text, f'{where}: give an example value')
                    if param['type'] == 'str' and not picked:
                        self.assertTrue(param.get('free'), f'{where}: give choices/choices_from, or free: why typed')

    def test_details_close_the_full_entry_and_never_the_list(self):
        cmd = command(name='x', summary='Plain words.', details='topics.effort and config/x.yaml')
        package = {'package': 'p', 'summary': '', 'commands': [cmd], 'path': ''}
        self.assertEqual(astro_help.details(cmd)[-1], '    topics.effort and config/x.yaml')
        self.assertNotIn('topics.effort', astro_help.text([package]))
        self.assertIn('topics.effort', astro_help.text([package], exact=('p', 'x')))

    def test_needs_and_conflicts_are_lists_shown_in_details(self):
        cmd = command(name='x', needs=['nav', 'drive+controller'], conflicts=[{'drive': 'it holds the pins'}])
        self.assertEqual(cmd['needs'], ['nav', 'drive+controller'])
        self.assertIn('starts first if down: nav, drive+controller; stops first if up: drive (--here skips this)',
                      '\n'.join(astro_help.details(cmd)))

if __name__ == '__main__':
    unittest.main()
