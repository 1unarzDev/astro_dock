#!/usr/bin/env python3
"""Clock offset of each LAN host against this one, by one SNTP query each.

    astro-clock.py [HOST ...]

Without hosts it asks BOAT_TIME_SERVER and every STATIC_LEASES host from the
vessel's network env (ASTRO_NETWORK_ENV, else `hw.sh env`, else the legacy
config/boat-network.env). Hosts set up by `setup.linux.sh --components clock`
with chrony answer; others show "no reply". Containers share the host clock,
so this runs the same inside one. Exit 1 when a host is off by more than
50 ms, unsynchronised, or silent.
"""
import os
import shlex
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_ENV = ROOT / 'config' / 'boat-network.env'
NTP_EPOCH = 2208988800  # seconds from 1900 to 1970
LIMIT = 0.050


def network_env():
    """The vessel's network env path: ASTRO_NETWORK_ENV, else `hw.sh env`, else the legacy file."""
    if os.environ.get('ASTRO_NETWORK_ENV'):
        return Path(os.environ['ASTRO_NETWORK_ENV'])
    run = subprocess.run([str(ROOT / 'hw.sh'), 'env'], capture_output=True, text=True)
    return Path(run.stdout.strip()) if run.returncode == 0 and run.stdout.strip() else LEGACY_ENV


def lan_hosts(path=None):
    """[(name, address)] from the env file: the time server, then the reservations."""
    path = path or network_env()
    values = {}
    for line in Path(path).read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and not line.lstrip().startswith('#'):
            values[key.strip()] = ' '.join(shlex.split(value, comments=True))
    hosts = [('time server', values['BOAT_TIME_SERVER'])] if values.get('BOAT_TIME_SERVER') else []
    for lease in values.get('STATIC_LEASES', '').split():
        _, name, address = lease.split('=')
        hosts.append((name, address))
    return hosts


def ntp_time(raw):
    seconds, fraction = struct.unpack('!II', raw)
    return seconds - NTP_EPOCH + fraction / 2 ** 32


def query(address, timeout=1.0):
    """One SNTP exchange; returns (offset, delay, stratum, leap, ref) in seconds."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t1 = time.time()
        sent = int(t1 + NTP_EPOCH)
        s.sendto(b'\x23' + bytes(39) + struct.pack('!II', sent, int((t1 % 1) * 2 ** 32)), (address, 123))
        data = s.recv(48)
        t4 = time.time()
    if len(data) < 48:
        raise OSError('short reply')
    t2, t3 = ntp_time(data[32:40]), ntp_time(data[40:48])
    stratum, ref = data[1], data[12:16]
    ref = ref.decode('ascii', 'replace').strip('\0') if stratum <= 1 else socket.inet_ntoa(ref)
    return ((t2 - t1) + (t3 - t4)) / 2, (t4 - t1) - (t3 - t2), stratum, data[0] >> 6, ref


def row(name, address):
    """One status line and whether the host is healthy."""
    try:
        offset, delay, stratum, leap, ref = query(address)
    except OSError as error:
        return f'{name:<18} {address:<15} no reply ({error})', False
    good = abs(offset) <= LIMIT and leap != 3 and 0 < stratum < 16
    state = 'ok' if good else 'unsynchronised' if leap == 3 or not 0 < stratum < 16 else 'OFF'
    return (f'{name:<18} {address:<15} {offset * 1e3:+9.1f} ms {delay * 1e3:7.1f} ms  '
            f'stratum {stratum:<2} ref {ref:<15} {state}'), good


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] in (['-h'], ['--help']):
        print(__doc__.strip())
        return 0
    if not argv and not network_env().is_file():
        print(f'astro-clock: no network env at {network_env()}; pass HOST arguments, '
              'set ASTRO_NETWORK_ENV or --vessel, or run git submodule update --init profiles', file=sys.stderr)
        return 1
    hosts = [(host, host) for host in argv] or lan_hosts()
    print(f'{"host":<18} {"address":<15} {"offset":>12} {"delay":>10}  vs {socket.gethostname()}')
    healthy = True
    for name, address in hosts:
        line, good = row(name, address)
        print(line)
        healthy &= good
    return 0 if healthy else 1


if __name__ == '__main__':
    sys.exit(main())
