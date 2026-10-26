"""hw.sh router against a fake DD-WRT (one file per NVRAM key) behind a fake ssh; no router is contacted."""
import base64
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(line.split('=', 1) for line in (ROOT / 'config/boat-network.env').read_text().splitlines()
           if line[:1].isalpha())

# Drops ssh options, logs the command and runs it with the fake nvram first on PATH.
FAKE_SSH = r'''#!/usr/bin/env bash
while [[ $1 == -* ]]; do [[ $1 == -[poi] ]] && shift; shift; done
shift
printf '%s\n' "$*" >>"$FAKE/ssh.log"
PATH="$FAKE/router:$PATH" exec sh -c "$*"
'''
FAKE_NVRAM = r'''#!/bin/sh
case $1 in
    get) [ -f "$FAKE/nv/$2" ] && cat "$FAKE/nv/$2"; exit 0 ;;
    set|unset|commit) echo "WRITE $*" >>"$FAKE/writes" ;;
    show) for f in "$FAKE"/nv/*; do printf '%s=%s\n' "${f##*/}" "$(cat "$f")"; done ;;
esac
'''


class Router(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for path, text in (('bin/ssh', FAKE_SSH), ('router/nvram', FAKE_NVRAM),
                           ('router/setuserpasswd', '#!/bin/sh\necho "WRITE setuserpasswd" >>"$FAKE/writes"\n'),
                           ('router/reboot', '#!/bin/sh\necho "WRITE reboot" >>"$FAKE/writes"\n')):
            (self.tmp / path).parent.mkdir(exist_ok=True)
            (self.tmp / path).write_text(text)
            (self.tmp / path).chmod(0o755)
        (self.tmp / 'nv').mkdir()
        for key, value in (('os_version', 'r59468'), ('wl0_ssid', 'old'), ('lan_ipaddr', ENV['ROUTER_IP'])):
            (self.tmp / 'nv' / key).write_text(value)
        state = self.tmp / 'state'
        state.mkdir(mode=0o700)
        key = self.tmp / 'hostkey'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
        (state / 'known_hosts').write_text('%s %s' % (ENV['ROUTER_IP'], (self.tmp / 'hostkey.pub').read_text()))

    def hw(self, verb, stdin=''):
        env = {**os.environ, 'PATH': '%s:%s' % (self.tmp / 'bin', os.environ['PATH']), 'FAKE': str(self.tmp),
               'ASTRO_ENV_FILE': str(self.tmp / 'no.env'), 'ROUTER_ADMIN_PASSWORD': 'adminpass',
               'BOAT_WIFI_PASSWORD': 'wifipass1', 'ROUTER_PASSWORD': ''}
        return subprocess.run(['bash', str(ROOT / 'hw.sh'), 'router', verb, '--state-dir', str(self.tmp / 'state')],
                              input=stdin, env=env, capture_output=True, text=True, timeout=60)

    def writes(self):
        path = self.tmp / 'writes'
        return path.read_text() if path.exists() else ''

    def test_status_plan_and_verify_change_nothing(self):
        run = self.hw('status')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Detected DD-WRT r59468 with radio(s): wl0', run.stdout)
        self.assertIn('lan_ipaddr=' + ENV['ROUTER_IP'], run.stdout)
        run = self.hw('plan')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Router name / SSID  : mhseals / mhseals', run.stdout)
        self.assertIn('Auto-revert         : 300s after boot', run.stdout)
        self.assertIn('Time zone           : America/Chicago', run.stdout)
        run = self.hw('verify')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('verification failed', run.stderr)
        self.assertEqual(self.writes(), '')

    def test_apply_needs_the_typed_confirmation(self):
        run = self.hw('apply', stdin='yes\n')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Cancelled without router changes', run.stdout)
        self.assertEqual(self.writes(), '')

    def test_old_command_maps_dry_run_to_plan(self):
        env = {**os.environ, 'PATH': '%s:%s' % (self.tmp / 'bin', os.environ['PATH']), 'FAKE': str(self.tmp),
               'ASTRO_ENV_FILE': str(self.tmp / 'no.env'), 'ROUTER_ADMIN_PASSWORD': 'adminpass',
               'BOAT_WIFI_PASSWORD': 'wifipass1', 'ROUTER_PASSWORD': ''}
        run = subprocess.run(['bash', str(ROOT / 'router_setup.sh'), '--dry-run', '--state-dir', str(self.tmp / 'state')],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('use: ./hw.sh router plan', run.stderr)
        self.assertIn('Plan only', run.stdout)
        self.assertEqual(self.writes(), '')
        (self.tmp / 'ssh.log').unlink()
        run = subprocess.run(['bash', str(ROOT / 'router_setup.sh')], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Usage:', run.stdout)
        self.assertFalse((self.tmp / 'ssh.log').exists())     # no arguments: usage, never apply

    def test_rollback_refuses_more_than_200_differing_keys(self):
        backups = self.tmp / 'state' / ENV['ROUTER_IP']
        backups.mkdir(mode=0o700, exist_ok=True)
        values = {'lan_ipaddr': ENV['ROUTER_IP'],
                  **{'extra_%d' % i: 'old' for i in range(201)}}
        (backups / 'nvram-20260101-000000.hex').write_text(''.join(
            '%s\t%s\n' % (key, (value.encode() + b'\n').hex())
            for key, value in values.items()))
        run = self.hw('rollback', stdin='1\nRESTORE\n')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('more than 200 differing keys', run.stderr)
        self.assertIn('rc_startup could overflow NVRAM', run.stderr)
        self.assertNotIn('Type RESTORE', run.stderr)
        self.assertNotIn('restored and confirmed', run.stdout)
        self.assertEqual(self.writes(), '')

    def test_rollback_refuses_a_bad_backup_before_contacting_the_router(self):
        b64 = lambda value: base64.b64encode(value.encode() + b'\n').decode()
        cases = {'empty': ('', 'is empty'),
                 'empty-value': ('lan_ipaddr\t%s\nwl0_ssid\t\n' % b64(ENV['ROUTER_IP']), 'not KEY<TAB>VALUE'),
                 'no-tab': ('lan_ipaddr\t%s\nwl0_ssid\n' % b64(ENV['ROUTER_IP']), 'not KEY<TAB>VALUE'),
                 'bad-ip': ('lan_ipaddr\t%s\nwl0_ssid\t%s\n' % (b64('192.168.1.300'), b64('old')), 'no valid lan_ipaddr'),
                 'no-ip': ('wl0_ssid\t%s\n' % b64('old'), 'no valid lan_ipaddr')}
        backups = self.tmp / 'state' / ENV['ROUTER_IP']
        backups.mkdir(mode=0o700, exist_ok=True)
        for name, (text, message) in cases.items():
            with self.subTest(backup=name):
                for old in backups.glob('nvram-*'):
                    old.unlink()
                (backups / 'nvram-20260101-000000.b64').write_text(text)
                run = self.hw('rollback', stdin='1\nRESTORE\n')
                self.assertNotEqual(run.returncode, 0)
                self.assertIn(message, run.stderr)
                self.assertNotIn('restored', run.stdout + run.stderr)
                self.assertFalse((self.tmp / 'ssh.log').exists())
                self.assertEqual(self.writes(), '')


if __name__ == '__main__':
    unittest.main()
