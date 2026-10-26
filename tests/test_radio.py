import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(line.split('=', 1) for line in (ROOT / 'config/boat-network.env').read_text().splitlines()
           if line[:1].isalpha())
ROCKET, BULLET, BOAT = ENV['UBNT_ROCKET_IP'], ENV['UBNT_BULLET_IP'], ENV['BOAT_TRANSPORT_IP']

# Drops ssh options, logs host and command, and runs the command in that host's directory, with
# the radio's /tmp, /usr/www and /proc/uptime mapped into it. An unknown host is unreachable.
FAKE_SSH = r'''#!/usr/bin/env bash
while [[ $1 == -o ]]; do shift 2; done
host=${1#*@}; shift
printf '%s %s\n' "$host" "$*" >>"$FAKE/ssh.log"
dir=$FAKE/hosts/$host
[[ -d $dir ]] || { echo "ssh: connect to host $host: No route to host" >&2; exit 255; }
cmd=$*
cmd=${cmd//\/tmp\//$dir/tmp/}
cmd=${cmd//\/usr\/www\//$dir/www/}
cmd=${cmd//\/proc\/uptime/$dir/uptime}
cd "$dir" && PATH="$dir/bin:$PATH" exec bash -c "$cmd"
'''
# One canned result per test kind; to-target TCP 40 Mbit/s, from-target 30.
FAKE_IPERF = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open(os.environ['FAKE'] + '/iperf.log', 'a').write(' '.join(a) + '\n')
if '--version' in a:
    print('iperf 3.16'); sys.exit(0)
if '-s' in a:
    sys.exit(0)
if '-u' in a:
    print(json.dumps({'end': {'sum': {'bits_per_second': 20e6, 'jitter_ms': 2.5, 'lost_percent': 0.4}}}))
else:
    rate = 30e6 if '-R' in a else 40e6
    print(json.dumps({'end': {'sum_sent': {'bits_per_second': rate, 'retransmits': 12},
                              'sum_received': {'bits_per_second': rate}}}))
'''
FAKE_PING = '''#!/bin/sh
echo "25 packets transmitted, 25 received, 0% packet loss, time 4800ms"
echo "rtt min/avg/max/mdev = 61.0/72.5/118.9/11.2 ms"
'''
FAKE_IP = '#!/bin/sh\necho "$4 dev lan0 src 192.168.0.2 uid 1000"\n'
# The container: iperf3 only once $FAKE/installed exists; an astro-ros-install call is logged.
FAKE_DOCKER = r'''#!/usr/bin/env bash
echo "docker $*" >>"$FAKE/docker.log"
[[ $1 == exec ]] || exit 1
shift
[[ $1 == -u ]] && shift 2
shift
[[ $1 == astro-ros-install ]] && { touch "$FAKE/installed"; exit 0; }
[[ -e $FAKE/installed ]] || exit 1
PATH="$FAKE/container:$PATH" exec "$@"
'''

STA = {'signal': -58, 'noisefloor': -95, 'tx_idx': 7, 'tx_nss': 2, 'rx_idx': 6, 'rx_nss': 2, 'tx_rate': 650,
       'rx_rate': 585, 'distance': 200, 'tx_latency': 3, 'tx_lretries': 12,
       'airmax': {'rx': {'cinr': 30}, 'tx': {'cinr': 29}, 'dl_capacity': 150000, 'ul_capacity': 140000}}
CFG = {'radio.1.freq': '5180', 'radio.1.center.1.freq': '5180', 'radio.1.chanbw': '20',
       'radio.1.ieee_mode': '11acvht20', 'radio.1.dfs.status': 'enabled', 'radio.1.polling': 'enabled',
       'radio.1.ptpmode': '0', 'radio.1.ack.auto': 'enabled', 'radio.1.ackdistance': '600',
       'radio.1.acktimeout': '31', 'radio.1.txpower': '24', 'tshaper.status': 'enabled',
       'wireless.1.ssid': 'blastoise', 'wireless.1.security.psk': 'secretkey', 'users.1.password': 'x'}


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


class LinkMeasureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for name, text in (('ssh', FAKE_SSH), ('iperf3', FAKE_IPERF), ('ping', FAKE_PING), ('ip', FAKE_IP)):
            executable(self.tmp / 'bin' / name, text)
        for ip, cfg in ((ROCKET, CFG), (BULLET, {**CFG, 'radio.1.chanbw': '40', 'wireless.1.scan_list.status': 'disabled'})):
            self.radio(ip, cfg)
        (self.tmp / 'hosts' / BOAT).mkdir(parents=True)
        self.state = self.tmp / 'state/astro_dock'

    def radio(self, ip, cfg):
        host = self.tmp / 'hosts' / ip
        (host / 'tmp').mkdir(parents=True, exist_ok=True)
        (host / 'tmp/system.cfg').write_text(''.join('%s=%s\n' % kv for kv in cfg.items()))
        (host / 'uptime').write_text('86400.12 1000.00\n')
        executable(host / 'bin/mca-status', '#!/bin/sh\necho "deviceName=r,firmwareVersion=XC.v8.7.1"\n'
                   'echo wlanPollingQuality=92\necho wlanPollingCapacity=78\n')
        executable(host / 'bin/wstalist', '#!/bin/sh\ncat <<"E"\n%s\nE\n' % json.dumps([STA]))
        status = {'wireless': {'frequency': 5180, 'center1_freq': 5180, 'chanbw': int(cfg['radio.1.chanbw']),
                               'txpower': 24, 'noisef': -95, 'distance': 200}}
        executable(host / 'www/status.cgi', '#!/bin/sh\nprintf "Content-Type: application/json\\n\\n"\n'
                   'cat <<"E"\n%s\nE\n' % json.dumps(status))

    def run_script(self, script, *args, stdin='', path_extra=(), **extra):
        env = {'PATH': ':'.join([*map(str, path_extra), str(self.tmp / 'bin'), os.environ['PATH']]),
               'HOME': str(self.tmp), 'XDG_STATE_HOME': str(self.tmp / 'state'), 'FAKE': str(self.tmp),
               'UBNT_PASSWORD': '', 'UBNT_JUMP': '', 'BOAT_HOST_USER': 'crew', 'BOAT_HOST_PASSWORD': '',
               'HW_MEASURE_SECONDS': '3', 'ASTRO_ENV_FILE': str(self.tmp / 'no.env')}
        env.update(extra)
        return subprocess.run(['bash', str(ROOT / script), *args], input=stdin, env=env,
                              capture_output=True, text=True, timeout=120)

    def hw(self, *args, **kwargs):
        return self.run_script('hw.sh', 'radio', *args, **kwargs)

    def ssh_log(self):
        path = self.tmp / 'ssh.log'
        return path.read_text().splitlines() if path.exists() else []

    def test_scripts_parse(self):
        subprocess.run(['bash', '-n', str(ROOT / 'devices/radio/airmax.sh')], check=True)

    def test_measure_reads_radios_runs_iperf_both_ways_and_saves(self):
        run = self.hw('measure')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertRegex(run.stdout, r'tcp to-target +40 Mbit/s +retrans 12 +\| ping avg 72.5 max 118.9 mdev 11.2')
        self.assertRegex(run.stdout, r'udp from-target +20 Mbit/s +jitter 2.5 ms, lost 0.4%')
        self.assertRegex(run.stdout, r'rocket +-58 +-95 +30/29 +92 +150000/140000 7/6')
        iperf = (self.tmp / 'iperf.log').read_text()
        self.assertIn('-u -b 28M', iperf)  # 70% of the 40 Mbit/s TCP rate
        self.assertIn('-u -b 21M -R', iperf)
        self.assertEqual(iperf.count('-s -1 -p 5201'), 4)
        raw, = (self.state / 'link').glob('link-*.txt')
        self.assertEqual(raw.stat().st_mode & 0o777, 0o600)
        text = raw.read_text()
        self.assertIn('skipped: an airOS survey', text)
        self.assertNotIn('secretkey', text)
        summary = json.loads(raw.with_suffix('.json').read_text())
        self.assertEqual(summary['radios']['bullet']['width'], 40)
        self.assertEqual(summary['radios']['rocket']['uptime_s'], 86400.12)
        self.assertEqual(summary['iperf']['tcp from-target']['mbps'], 30)
        # Radios get status reads only, never a write, reboot or scan.
        radio_cmds = [l for l in self.ssh_log() if l.startswith((ROCKET + ' ', BULLET + ' '))]
        self.assertEqual(len(radio_cmds), 2)
        for word in ('softrestart', 'cfgmtd', 'reboot', 'iwlist', 'survey', '>/tmp', 'cp '):
            self.assertFalse(any(word in l for l in radio_cmds), word)

    def test_measure_uses_the_container_iperf3_and_never_installs_one(self):
        (self.tmp / 'bin/iperf3').rename(self.tmp / 'container-iperf3')
        executable(self.tmp / 'bin/docker', FAKE_DOCKER)
        run = self.hw('measure')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('iperf3: not on', run.stderr)
        self.assertIn('install it (astro-ros-install iperf3)', run.stderr)
        self.assertNotIn('astro-ros-install', (self.tmp / 'docker.log').read_text())
        executable(self.tmp / 'container/iperf3', (self.tmp / 'container-iperf3').read_text())
        (self.tmp / 'installed').touch()
        run = self.hw('measure')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertRegex(run.stdout, r'tcp to-target +40 Mbit/s')
        self.assertNotIn('astro-ros-install', (self.tmp / 'docker.log').read_text())

    def test_measure_needs_the_boat_login(self):
        run = self.hw('measure', BOAT_HOST_USER='')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('BOAT_HOST_USER (the login on', run.stderr)
        self.assertEqual(self.ssh_log(), [])

    def test_measure_needs_a_route_to_the_boat(self):
        executable(self.tmp / 'bin/ip', '#!/bin/sh\nexit 2\n')
        run = self.hw('measure')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('No route', run.stderr)
        self.assertEqual(self.ssh_log(), [])

    def write_measurement(self, **radio_edits):
        radios = {}
        for name, role, signal in (('rocket', 'ap', -45), ('bullet', 'station', -46)):
            cfg = dict(CFG)
            if name == 'bullet':
                cfg.pop('tshaper.status')
                cfg['radio.1.txpower'] = '25'
            radios[name] = {'role': role, 'signal': signal, 'noise': -95, 'distance_m': 200,
                            'txpower': float(cfg['radio.1.txpower']), 'cfg': cfg}
            for key, value in radio_edits.get(name, {}).items():
                (radios[name]['cfg'] if '.' in key else radios[name])[key] = value
        path = self.tmp / 'm.json'
        path.write_text(json.dumps({'radios': radios}))
        return str(path)

    def test_propose_widens_a_quiet_link_and_tunes_both_radios(self):
        run = self.hw('propose', self.write_measurement())
        self.assertEqual(run.returncode, 0, run.stderr)
        out = run.stdout
        for line in ('radio.1.chanbw +20 -> 40', r'radio.1.center.1.freq +5180 -> 5190', 'radio.1.ieee_mode +11acvht20 -> 11acvht40',
                     'radio.1.ptpmode +0 -> 1', 'radio.1.ack.auto +enabled -> disabled', 'radio.1.ackdistance +600 -> 1000',
                     'radio.1.acktimeout +31 -> 34', 'radio.1.txpower +24 -> 20', 'tshaper.status +enabled -> disabled'):
            self.assertRegex(out, line)
        self.assertRegex(out.split('bullet (station)')[1], r'radio.1.txpower +25 -> 20')
        self.assertNotIn('chanbw', out.split('bullet (station)')[1].split('note:')[0])
        self.assertIn('plan rocket radio.1.center.1.freq=5190 radio.1.chanbw=40 radio.1.ieee_mode=11acvht40\n', out)
        self.assertIn('bullet: tshaper.status is not in its config', out)
        self.assertLess(out.index('plan rocket radio.1.center'), out.index('plan bullet'))

    def test_propose_narrows_a_noisy_link_and_never_raises_power_unasked(self):
        weak = {'signal': -72, 'noise': -82, 'radio.1.chanbw': '40', 'radio.1.center.1.freq': '5190',
                'radio.1.ieee_mode': '11acvht40'}
        path = self.write_measurement(rocket=weak, bullet={'signal': -72, 'noise': -82})
        run = self.hw('propose', path)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertRegex(run.stdout, 'radio.1.chanbw +40 -> 20')
        self.assertRegex(run.stdout, r'radio.1.center.1.freq +5190 -> 5180')
        self.assertNotIn('txpower', run.stdout.split('note:')[0])
        self.assertIn('raise power only with propose.txpower_max', run.stdout)
        run = self.hw('propose', path, HW_PROPOSE_TXPOWER_MAX='26')
        self.assertRegex(run.stdout, r'radio.1.txpower +24 -> 26')

    def test_propose_guards_channel_choices(self):
        run = self.hw('propose', self.write_measurement(), HW_PROPOSE_FREQ='5300')
        self.assertRegex(run.stdout, r'radio.1.freq +5180 -> 5300')
        self.assertIn('DFS channel', run.stdout)
        off_grid = self.write_measurement(rocket={'radio.1.freq': '5165', 'radio.1.center.1.freq': '5165'})
        run = self.hw('propose', off_grid)
        self.assertNotIn('chanbw', run.stdout.split('note:')[0])
        self.assertIn('off the 802.11 grid', run.stdout)
        for key, value in (('HW_PROPOSE_FREQ', '2412'), ('HW_PROPOSE_RANGE_M', '50'), ('HW_PROPOSE_WIDTH_MAX', '60'), ('HW_PROPOSE_FREQ', '5180;x')):
            with self.subTest(key=key):
                run = self.hw('propose', self.write_measurement(), **{key: value})
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('link proposal', run.stderr)

    def test_propose_without_measurement(self):
        run = self.hw('propose')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('run hw.sh radio measure first', run.stderr)

    def radio_files(self, ip):
        return sorted(p.name for p in (self.tmp / 'hosts' / ip / 'tmp').iterdir())

    def test_apply_dry_run_shows_the_diff_and_changes_nothing(self):
        run = self.hw('apply', 'rocket', '--dry-run', 'radio.1.chanbw=40',
                              'radio.1.txpower=20')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('radio.1.chanbw: 20 -> 40', run.stdout)
        self.assertIn('radio.1.txpower: 24 -> 20', run.stdout)
        self.assertIn('re-scans', run.stderr)
        self.assertIn('Dry run; nothing applied or saved', run.stdout)
        self.assertNotIn('Type APPLY', run.stdout)
        self.assertEqual(self.radio_files(ROCKET), ['system.cfg'])
        self.assertIn('radio.1.chanbw=20', (self.tmp / 'hosts' / ROCKET / 'tmp/system.cfg').read_text())
        self.assertEqual(list((self.state / 'ubiquiti').glob('rocket-*.cfg')), [])   # no secret-bearing snapshot

    def test_apply_refuses_channel_keys_on_the_station(self):
        run = self.hw('apply', 'bullet', '--dry-run', 'radio.1.chanbw=20')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('link-shared key', run.stderr)

    def test_apply_refuses_a_channel_outside_the_station_scan_list(self):
        self.radio(BULLET, {**CFG, 'wireless.1.scan_list.status': 'enabled', 'wireless.1.scan_list.channels': '5180,5200'})
        run = self.hw('apply', 'rocket', '--dry-run', 'radio.1.freq=5745')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('scan list does not include 5745', run.stderr)
        run = self.hw('apply', 'rocket', '--dry-run', 'radio.1.freq=5200')
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_keep_is_refused_when_the_peer_is_lost_on_the_trial_channel(self):
        rocket = self.tmp / 'hosts' / ROCKET
        executable(rocket / 'bin/reboot', '#!/bin/sh\ntouch "$FAKE/rebooted"\n')
        # The trial's config copy drops the station, as a channel it cannot follow would.
        executable(rocket / 'bin/cp', '#!/bin/sh\nrm -rf "$FAKE/hosts/%s"\nexec /bin/cp "$@"\n' % BULLET)

        def stop_timer():
            pid = (rocket / 'tmp/astro-trial.pid').read_text().strip()
            subprocess.run(['kill', pid])
            subprocess.run(['pkill', '-P', pid])
        self.addCleanup(stop_timer)
        run = self.hw('apply', 'rocket', 'radio.1.chanbw=40', stdin='APPLY\nKEEP\n')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('bullet radio is unreachable on the trial channel/width; not saved', run.stderr)
        self.assertFalse(any('cfgmtd' in l for l in self.ssh_log()))
        self.assertTrue((rocket / 'tmp/astro-trial.pid').exists())      # the revert timer still runs

    def test_rollback_reboots_only_a_radio_with_a_pending_trial(self):
        run = self.hw('rollback', 'rocket', stdin='ROLLBACK\n')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('no pending trial', run.stderr)
        self.assertFalse(any('reboot' in l for l in self.ssh_log()))
        rocket = self.tmp / 'hosts' / ROCKET
        executable(rocket / 'bin/reboot', '#!/bin/sh\ntouch "$FAKE/rebooted"\n')
        (rocket / 'tmp/astro-trial.pid').write_text(str(os.getpid()))
        run = self.hw('rollback', 'rocket', stdin='ROLLBACK\n')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue((self.tmp / 'rebooted').exists())

    def test_status_is_read_only_and_drops_secrets(self):
        run = self.hw('status', 'rocket')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('wlanPollingQuality=92', run.stdout)
        self.assertIn('"center1_freq": 5180', run.stdout)
        self.assertIn('radio.1.chanbw=20', run.stdout)
        self.assertNotIn('secretkey', run.stdout)
        self.assertNotIn('users.1.password', run.stdout)


if __name__ == '__main__':
    unittest.main()
