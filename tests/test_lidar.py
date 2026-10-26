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
down = state_path + '.down'  # GETs left that fail, as while the sensor restarts
if os.path.exists(down):
    left = int(open(down).read())
    if left > 0:
        open(down, 'w').write(str(left - 1))
        sys.exit(7)
args = sys.argv[1:]
url = next(a for a in args if a.startswith('http://'))
assert url.startswith('http://' + os.environ['EXPECT_IP'] + '/cgi/'), url
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
    open(down, 'w').write(os.environ.get('FAKE_DOWN_POLLS', '1'))
json.dump(state, open(state_path, 'w'))
if path == 'reset':
    sys.exit(int(os.environ.get('FAKE_RESET_RC', '0')))
'''
FAKE_IP = '#!/bin/sh\necho "$4 dev lan0 src 192.168.0.2 uid 1000"\n'
# Drops ssh options, logs the host, and runs the quoted remote command locally.
FAKE_SSH = '''#!/usr/bin/env bash
while [[ $1 == -o ]]; do shift 2; done
echo "$1" >>"$FAKE_LIDAR.ssh"
shift
eval "$*"
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
        for name, text in (('curl', FAKE_CURL), ('ip', FAKE_IP), ('ssh', FAKE_SSH)):
            (self.tmp / 'bin' / name).write_text(text)
            (self.tmp / 'bin' / name).chmod(0o755)
        self.state = self.tmp / 'lidar.json'
        self.set_sensor(sensor())
        self.log = Path(str(self.state) + '.log')

    def set_sensor(self, state):
        self.state.write_text(json.dumps(state))

    def edit_sensor(self, group, **values):
        state = sensor()
        for name in ([group] if group == 'host' else ['net', 'active_net']):
            state['settings'][name].update(values)
        self.set_sensor(state)

    def run_script(self, verb, stdin='', script='hw.sh', *args, **extra):
        env = {'PATH': '%s:%s' % (self.tmp / 'bin', os.environ['PATH']), 'HOME': str(self.tmp),
               'XDG_STATE_HOME': str(self.tmp / 'state'), 'FAKE_LIDAR': str(self.state), 'LIDAR_JUMP': '',
               'EXPECT_IP': ENV['LIDAR_IP'], 'ASTRO_ENV_FILE': str(self.tmp / 'no.env')}
        env.update(extra)
        command = ['lidar', verb] if script == 'hw.sh' else [verb]
        return subprocess.run(['bash', str(ROOT / script), *command, *args], input=stdin, env=env,
                              capture_output=True, text=True, timeout=120)

    def posts(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def assert_net_apply(self, run, *net_posts):
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.posts(), ['setting/host addr=' + ENV['LIDAR_DATA_HOST'], *net_posts,
                                        'save submit', 'reset reset_system'])
        self.assertIn('Matches the profile', run.stdout)

    def test_script_parses(self):
        subprocess.run(['bash', '-n', str(ROOT / 'devices/lidar/vlp16.sh')], check=True)

    def test_verify_reports_only_differing_keys(self):
        run = self.run_script('verify')
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assertRegex(run.stderr, r'host\.addr +192\.168\.0\.152 -> ' + ENV['LIDAR_DATA_HOST'])
        self.assertRegex(run.stderr, r'net\.gateway +192\.168\.0\.3 -> ' + ENV['ROUTER_IP'])
        self.assertNotIn('rpm', run.stderr.split('Differs')[1])
        self.assertEqual(self.posts(), [])

    def test_apply_posts_diffs_network_last_then_one_save_and_reset(self):
        run = self.run_script('apply', 'APPLY\n')
        self.assert_net_apply(run, 'setting/net gateway=' + ENV['ROUTER_IP'])
        snapshots = list((self.tmp / 'state/astro_dock/lidar').iterdir())
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(snapshots[0].read_text())['settings']['host']['addr'], '192.168.0.152')
        self.assertEqual(self.run_script('apply').returncode, 0)
        self.assertEqual(len(self.posts()), 4)

    def test_apply_without_network_change_saves_once_without_reset(self):
        self.edit_sensor('net', gateway=ENV['ROUTER_IP'])
        run = self.run_script('apply', 'APPLY\n')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.posts(), ['setting/host addr=' + ENV['LIDAR_DATA_HOST'], 'save submit'])

    def test_apply_through_jump_host(self):
        run = self.run_script('apply', 'APPLY\n', LIDAR_JUMP='tranquility')
        self.assert_net_apply(run, 'setting/net gateway=' + ENV['ROUTER_IP'])
        self.assertEqual(set(Path(str(self.state) + '.ssh').read_text().split()), {'tranquility'})

    def test_apply_changes_mask_and_gateway(self):
        self.edit_sensor('net', mask='255.255.0.0')
        run = self.run_script('apply', 'APPLY\n')
        self.assert_net_apply(run, 'setting/net mask=' + ENV['NETMASK'],
                              'setting/net gateway=' + ENV['ROUTER_IP'])

    def test_apply_waits_for_a_slow_reset(self):
        run = self.run_script('apply', 'APPLY\n', FAKE_DOWN_POLLS='4')
        self.assert_net_apply(run, 'setting/net gateway=' + ENV['ROUTER_IP'])

    def test_apply_tolerates_reset_connection_drop(self):
        run = self.run_script('apply', 'APPLY\n', FAKE_RESET_RC='52')
        self.assert_net_apply(run, 'setting/net gateway=' + ENV['ROUTER_IP'])

    def test_apply_dies_when_reset_leaves_old_network_active(self):
        state = sensor()
        state['settings']['net']['gateway'] = ENV['ROUTER_IP']  # saved, never activated by our fake
        self.set_sensor(state)
        fake = self.tmp / 'bin/curl'
        fake.write_text(fake.read_text().replace("settings['active_net'] = dict(settings['net'])", 'pass'))
        run = self.run_script('apply', 'APPLY\n')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('without the new netmask/gateway active', run.stderr)

    def test_apply_refuses_dhcp_on_with_network_change(self):
        self.edit_sensor('net', dhcp='On')
        run = self.run_script('apply', 'APPLY\n')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('DHCP is on', run.stderr)
        self.assertEqual(self.posts(), [])

    def test_apply_refuses_address_mismatch(self):
        self.edit_sensor('net', addr='192.168.0.202')
        run = self.run_script('apply', 'APPLY\n')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('reports address 192.168.0.202', run.stderr)
        self.assertEqual(self.posts(), [])

    def test_apply_refuses_invalid_values(self):
        for key, value in (('HW_RPM', '610'), ('HW_RPM', '600&laser=off'), ('HW_RETURNS', 'Dual&rpm=0'),
                           ('HW_DATA_HOST', '192.168.0.300'), ('HW_DATA_PORT', '70000'),
                           ('HW_FOV_END', '360'), ('HW_GATEWAY', '10.0.0.1'),
                           ('HW_NETMASK', '255.0.255.0'), ('HW_NETMASK', '255.255.255.252'),
                           ('HW_DATA_HOST', '192.168.1.153')):
            with self.subTest(key=key, value=value):
                run = self.run_script('apply', 'APPLY\n', **{key: value})
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('lidar config', run.stderr)
                self.assertNotIn('Type APPLY', run.stdout)
                self.assertEqual(self.posts(), [])

    def test_apply_without_typed_confirmation_posts_nothing(self):
        run = self.run_script('apply', 'yes\n')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Cancelled', run.stdout)
        self.assertEqual(self.posts(), [])

    def test_plan_and_status_post_nothing(self):
        run = self.run_script('plan')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertRegex(run.stdout, r'data_host +host\.addr +192\.168\.0\.152 -> ' + ENV['LIDAR_DATA_HOST'])
        self.assertEqual(self.run_script('status').returncode, 0)
        self.assertEqual(self.posts(), [])
        self.assertFalse((self.tmp / 'state').exists())

    def test_rollback_reapplies_a_snapshot(self):
        self.assertEqual(self.run_script('apply', 'APPLY\n').returncode, 0)
        self.log.unlink()
        run = self.run_script('rollback', 'APPLY\n')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.posts(), ['setting/host addr=192.168.0.152', 'setting/net gateway=192.168.0.3',
                                        'save submit', 'reset reset_system'])

    def test_apply_refuses_unreachable_lidar(self):
        Path(str(self.state) + '.down').write_text('999')
        run = self.run_script('apply', 'APPLY\n')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('No settings from the lidar', run.stderr)
        self.assertEqual(self.posts(), [])
        self.assertFalse((self.tmp / 'state').exists())


if __name__ == '__main__':
    unittest.main()
