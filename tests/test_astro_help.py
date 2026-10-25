import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('astro_help', ROOT / '.devcontainer' / 'astro-help.py')
astro_help = importlib.util.module_from_spec(spec)
spec.loader.exec_module(astro_help)

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
        self.assertEqual([p['package'] for p in packages], ['astro_dock', 'alpha', 'beta'])
        self.assertEqual(packages[1]['commands'][0]['name'], 'from_source')
        self.assertEqual(len(errors), 2)  # the unclosed list and the nameless package
        self.assertTrue(all(e.startswith('help: ') for e in errors))

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

    def test_text_lists_and_details(self):
        packages = [astro_help.parse(astro_help.OWN)]
        overview = astro_help.text(packages)
        self.assertIn('astro_dock · ', overview)
        self.assertNotIn('$ ', overview)
        detail = astro_help.text(packages, 'apt')
        self.assertIn('$ astro-ros-install <names>', detail)
        self.assertNotIn('ros_env', detail)
        self.assertIn('No command matches', astro_help.text(packages, 'zzz'))


if __name__ == '__main__':
    unittest.main()
