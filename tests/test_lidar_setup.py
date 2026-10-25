import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(line.split('=', 1) for line in (ROOT / 'config/boat-network.env').read_text().splitlines()
           if line[:1].isalpha())

# A curl stand-in serving one VLP16's JSON and applying POSTs the way the sensor does.
FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ['FAKE_LIDAR']
if os.path.exists(state_path + '.down'):
    sys.exit(7)
args = sys.argv[1:]
url = next(a for a in args if a.startswith('http://'))
path = url.split('/cgi/', 1)[1]
state = json.load(open(state_path))
if '--data' not in args:
    print(json.dumps(state.get(path[:-len('.json')], {})))
    sys.exit(0)
form = args[args.index('--data') + 1]
with open(state_path + '.log', 'a') as log:
    log.write('%s %s\n' % (path, form))
settings = state['settings']
if path.startswith('setting'):
    key, value = form.split('=', 1)
    group = path[len('setting/'):] if '/' in path else ''
    target = settings[group] if group else settings
    target[key] = int(value) if key == 'rpm' else value
elif path == 'reset':
    settings['active_net'] = dict(settings['net'])
json.dump(state, open(state_path, 'w'))
'''


def sensor():
    net = {'addr': ENV['LIDAR_IP'], 'mask': '255.255.255.0', 'gateway': '192.168.0.3', 'dhcp': 'Off',
           'mac_addr': '60-76-88-38-35-85'}
    return {'settings': {'laser': 'On', 'returns': 'Strongest', 'rpm': 600, 'fov': {'start': 0, 'end': 359},
                         'phaselock': {'enabled': 'Off', 'offset': '0'},
                         'host': {'addr': '192.168.0.152', 'dport': '2368', 'tport': '8308', 'udpcs': 'Off'},
                         'net': net, 'active_net': dict(net)},
            'status': {'gps': {'pps_state': 'Absent'}, 'motor': {'state': 'On', 'rpm': 600, 'lock': 'Off'},
                       'laser': {'state': 'On'}},
            'info': {'model': 'VLP-16-A', 'serial': '1', 'build': {'bot': 'hotfix/3.0.41.1#1'}}}


class LidarSetupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / 'bin').mkdir()
        curl = self.tmp / 'bin/curl'
        curl.write_text(FAKE_CURL)
        curl.chmod(0o755)
        self.state = self.tmp / 'lidar.json'
        self.state.write_text(json.dumps(sensor()))
        self.log = Path(str(self.state) + '.log')

    def run_script(self, verb, stdin=''):
        env = {'PATH': '%s:%s' % (self.tmp / 'bin', os.environ['PATH']), 'HOME': str(self.tmp),
               'XDG_STATE_HOME': str(self.tmp / 'state'), 'FAKE_LIDAR': str(self.state), 'LIDAR_JUMP': ''}
        return subprocess.run(['bash', str(ROOT / 'lidar_setup.sh'), verb], input=stdin, env=env,
                              capture_output=True, text=True)

    def posts(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_script_parses(self):
        subprocess.run(['bash', '-n', str(ROOT / 'lidar_setup.sh')], check=True)

    def test_verify_reports_only_differing_keys(self):
        run = self.run_script('verify')
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assertIn('host.addr    192.168.0.152 -> ' + ENV['LIDAR_DATA_HOST'], run.stderr)
        self.assertIn('net.gateway  192.168.0.3 -> ' + ENV['LIDAR_GATEWAY'], run.stderr)
        self.assertNotIn('rpm', run.stderr.split('Differs')[1])
        self.assertEqual(self.posts(), [])

    def test_apply_posts_diffs_network_last_then_verifies(self):
        run = self.run_script('apply', 'APPLY\n')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.posts(), ['setting/host addr=' + ENV['LIDAR_DATA_HOST'], 'save submit',
                                        'setting/net gateway=' + ENV['LIDAR_GATEWAY'], 'save submit',
                                        'reset reset_system'])
        self.assertIn('Matches config/boat-network.env', run.stdout)
        snapshots = list((self.tmp / 'state/astro_dock/lidar').iterdir())
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(snapshots[0].read_text())['settings']['host']['addr'], '192.168.0.152')
        self.assertEqual(self.run_script('apply').returncode, 0)
        self.assertEqual(len(self.posts()), 5)

    def test_apply_without_typed_confirmation_posts_nothing(self):
        run = self.run_script('apply', 'yes\n')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Cancelled', run.stdout)
        self.assertEqual(self.posts(), [])

    def test_apply_refuses_unreachable_lidar(self):
        Path(str(self.state) + '.down').touch()
        run = self.run_script('apply', 'APPLY\n')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('No settings from the lidar', run.stderr)
        self.assertEqual(self.posts(), [])
        self.assertFalse((self.tmp / 'state').exists())


if __name__ == '__main__':
    unittest.main()
