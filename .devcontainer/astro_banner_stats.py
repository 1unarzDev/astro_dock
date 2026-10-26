"""Telemetry rows for astro_banner.py: cheap reads inline, slow probes from a cache.

Git, the ROS graph and LAN probes run in a detached child that rewrites the cache
in the runtime dir, so a banner shows the last value and never waits on them.
"""
import json
import os
from pathlib import Path
import re
import time


# ---------------------------------------------------------------- cache

def cache_path(env):
    # $XDG_RUNTIME_DIR can name a directory that does not exist (a container, su, ssh):
    # a refresher that cannot write there would leave every probe row pending for good.
    run = env.get('XDG_RUNTIME_DIR') or '/tmp'
    if not (os.path.isdir(run) and os.access(run, os.W_OK)):
        run = '/tmp'
    return Path(run) / ('astro-banner-%d.json' % os.getuid())


def read_cache(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def stale(cache, name, key, ttl, now):
    entry = cache.get(name)
    return not isinstance(entry, dict) or entry.get('key') != key or now - entry.get('t', 0) > ttl


def probe_repos(ws):
    import subprocess
    roots = [ws] + sorted(p.parent for p in (ws / 'src').glob('*/.git')) + sorted(
        p.parent for p in (ws / 'src').glob('*/*/.git'))
    repos = []
    for root in roots:
        if not (root / '.git').exists():
            continue
        try:
            out = subprocess.run(['git', '-C', str(root), 'status', '--porcelain=v1', '-b'],
                                 capture_output=True, text=True, timeout=3).stdout.splitlines()
        except subprocess.TimeoutExpired:
            continue  # a huge repo costs its own row, not the rest
        head = out[0] if out else ''
        ahead = re.search(r'ahead (\d+)', head)
        repos.append({'name': root.name, 'dirty': len(out) - 1 if out else 0,
                      'ahead': int(ahead.group(1)) if ahead else 0})
    return repos


def probe_nodes(env):
    import subprocess
    try:
        out = subprocess.run(['ros2', 'node', 'list'], capture_output=True, text=True, timeout=5, env=env)
    except FileNotFoundError:
        return -1
    return len(set(line for line in out.stdout.splitlines() if line.startswith('/')))


def probe_lan(hosts):
    import socket
    up = {}
    for name, target in hosts.items():
        host, _, port = str(target).partition(':')
        try:
            socket.create_connection((host, int(port or 22)), 0.5).close()
            up[name] = True
        except (OSError, ValueError):
            up[name] = False
    return up


def refresh(path, jobs):
    """Run stale probes in a detached grandchild; the caller returns at once."""
    lock = Path(str(path) + '.lock')
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:  # a refresher that died leaves its lock; take it over after a minute
            if time.time() - lock.stat().st_mtime < 60:
                return
            lock.unlink()
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError:
            return
    except OSError:
        return
    pid = os.fork()
    if pid:
        os.close(fd)
        os.waitpid(pid, 0)
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        null = os.open(os.devnull, os.O_RDWR)
        for stream in (0, 1, 2):
            os.dup2(null, stream)
        cache = read_cache(path)
        for name, key, probe in jobs:
            try:
                cache[name] = {'t': time.time(), 'key': key, 'value': probe()}
            except Exception:
                cache[name] = {'t': time.time(), 'key': key, 'value': None}
            tmp = Path('%s.%d' % (path, os.getpid()))  # per job: a slow probe holds back no other row
            tmp.write_text(json.dumps(cache))
            os.replace(str(tmp), str(path))
    finally:
        try:
            lock.unlink()
        finally:
            os._exit(0)


# ---------------------------------------------------------------- stats
# Each returns a list of (text, tone) segments, or None to hide the row.

def read(path, default=''):
    try:
        return Path(path).read_text(errors='replace')
    except OSError:
        return default


def human(n):
    for unit in 'BKMGT':
        if n < 1024 or unit == 'T':
            return ('%.0f%s' if n >= 10 else '%.1f%s') % (n, unit)
        n /= 1024.0


def ago(seconds):
    s = int(max(seconds, 0))
    return '%ds' % s if s < 90 else '%dm' % (s // 60) if s < 5400 else '%dh' % (s // 3600) if s < 172800 \
        else '%dd' % (s // 86400)


def stat_ros(c):
    env = c['env']
    if not env.get('ROS_DISTRO'):
        return [('ROS not sourced', 'bad')]
    rmw = env.get('RMW_IMPLEMENTATION') or 'rmw_fastrtps_cpp'
    rmw = rmw.replace('rmw_', '').replace('_cpp', '')
    return [(env['ROS_DISTRO'], None), ('domain %s' % env.get('ROS_DOMAIN_ID', '0'), None), (rmw, None),
            (env.get('ROS_AUTOMATIC_DISCOVERY_RANGE', 'SUBNET').lower(), 'dim')]


def stat_dds(c):
    env = c['env']
    if env.get('ROS_AUTOMATIC_DISCOVERY_RANGE', '').upper() == 'LOCALHOST':
        return [('localhost only', 'warn')]
    if env.get('ASTRO_DDS_ADDRESS'):
        return [('pinned %s' % env['ASTRO_DDS_ADDRESS'], None)]
    return [('all interfaces', 'warn'), ('not pinned to a subnet address', 'dim')]


def stat_host(c):
    u = os.uname()
    where = 'container' if os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv') else 'host'
    return [(u.nodename, None), (u.machine, None), (u.release.split('-')[0], None), (where, 'dim')]


def stat_hw(c):
    model = read('/proc/device-tree/model').strip('\x00\n ')
    segments = []
    if model:
        segments.append((model.replace('NVIDIA ', ''), None))
        mode = re.search(r'pmode:(\d+)', read('/var/lib/nvpmodel/status'))
        if mode:
            names = dict(re.findall(r'POWER_MODEL\s+ID=(\d+)\s+NAME=(\S+)', read('/etc/nvpmodel.conf')))
            segments.append(('power %s' % names.get(str(int(mode.group(1))), mode.group(1)), None))
    else:
        cpu = re.search(r'model name\s*:\s*(.+)', read('/proc/cpuinfo'))
        if cpu:
            name = re.sub(r'\(R\)|\(TM\)|CPU|Processor|@.*', '', cpu.group(1))
            segments.append((' '.join(name.split()), None))
        for info in sorted(Path('/proc/driver/nvidia/gpus').glob('*/information')):
            gpu = re.search(r'Model:\s*(.+)', read(info))
            if gpu:
                segments.append((gpu.group(1).strip().replace('NVIDIA ', ''), None))
    segments.append(('%d cpus' % (os.cpu_count() or 0), 'dim'))
    return segments


def find_packages(src, depth=4):
    """Package names under src, pruned like colcon: stop at package.xml, skip *_IGNORE."""
    names, stack = [], [(str(src), 0)]
    while stack:
        path, level = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        files = set(e.name for e in entries)
        if files & {'COLCON_IGNORE', 'AMENT_IGNORE', 'CATKIN_IGNORE'}:
            continue
        if 'package.xml' in files:
            name = re.search(r'<name>\s*([^<\s]+)', read(os.path.join(path, 'package.xml'))[:4096])
            names.append(name.group(1) if name else os.path.basename(path))
        elif level < depth:
            stack.extend((e.path, level + 1) for e in entries if e.is_dir() and not e.name.startswith('.'))
    return names


def stat_workspace(c):
    ws, home = c['ws'], str(Path.home())
    names = find_packages(ws / 'src')
    install = ws / 'install'
    built = sum(1 for n in names if (install / n).is_dir() or (install / 'share' / n).is_dir())
    label = str(ws).replace(home, '~', 1) if str(ws).startswith(home) else str(ws)
    segments = [(label, None), ('%d pkgs' % len(names), None),
                ('%d built' % built, None if built == len(names) else 'warn')]
    try:
        segments.append(('built %s ago' % ago(c['now'] - (ws / 'log' / 'latest_build').stat().st_mtime), 'dim'))
    except OSError:
        segments.append(('never built', 'dim'))
    return segments


def cached(c, name):
    entry = c['cache'].get(name)
    return entry if isinstance(entry, dict) else None


def stat_repos(c):
    entry = cached(c, 'repos')
    if entry is None:
        return [('pending', 'dim')]
    repos = entry.get('value') or []
    dirty = [r['name'] for r in repos if r['dirty']]
    ahead = [r['name'] for r in repos if r['ahead']]
    segments = [('%d repo%s' % (len(repos), '' if len(repos) == 1 else 's'), None)]
    if not dirty and not ahead:
        segments.append(('clean', None))
    if dirty:
        segments.append(('✗ ' + ' '.join(dirty), 'warn'))
    if ahead:
        segments.append(('↑ ' + ' '.join(ahead), None))
    return segments


def stat_nodes(c):
    entry = cached(c, 'nodes')
    if entry is None or entry.get('key') != c['keys']['nodes']:
        return [('pending', 'dim')] if c['env'].get('ROS_DISTRO') else None
    if entry.get('value') is None:
        return [('graph unavailable', 'dim')]
    count = entry['value']
    if count < 0:
        return None
    return [('%d nodes' % count if count else 'no nodes', None if count else 'dim'),
            ('domain %s' % c['env'].get('ROS_DOMAIN_ID', '0'), None),
            ('%s ago' % ago(c['now'] - entry.get('t', 0)), 'dim')]


def stat_time(c):
    import ctypes

    class Timex(ctypes.Structure):  # struct timex up to status; the rest is padding
        _fields_ = [('modes', ctypes.c_uint), ('offset', ctypes.c_long), ('freq', ctypes.c_long),
                    ('maxerror', ctypes.c_long), ('esterror', ctypes.c_long), ('status', ctypes.c_int),
                    ('pad', ctypes.c_char * 256)]
    tx = Timex()
    state = ctypes.CDLL(None, use_errno=True).adjtimex(ctypes.byref(tx))
    if state < 0:
        return None
    if state == 5 or tx.status & 0x40:  # TIME_ERROR or STA_UNSYNC
        return [('unsynchronized', 'bad'), ('max error %d ms' % (tx.maxerror // 1000), 'dim')]
    offset = abs(tx.offset) / (1e6 if tx.status & 0x2000 else 1e3)  # STA_NANO: ns, else us
    return [('synced', None), ('offset %.2f ms' % offset, None), ('est ±%.1f ms' % (tx.esterror / 1e3), 'dim')]


def stat_disk(c):
    path = c['config'].get('disk') or str(c['ws'])
    while path and not os.path.exists(path):
        path = os.path.dirname(path)
    fs = os.statvfs(path)
    total, free = fs.f_blocks * fs.f_frsize, fs.f_bavail * fs.f_frsize
    used = 1 - free / float(total or 1)
    tone = 'bad' if used > 0.9 else 'warn' if used > 0.8 else None
    bar = '▰' * int(round(used * 8)) + '▱' * (8 - int(round(used * 8)))
    return [(os.path.basename(path.rstrip('/')) or '/', None),
            ('%s free of %s' % (human(free), human(total)), None), (bar + ' %d%%' % (used * 100), tone)]


def stat_system(c):
    up = float(read('/proc/uptime', '0').split()[0])
    mem = dict(re.findall(r'(\w+):\s+(\d+)', read('/proc/meminfo')))
    total, avail = int(mem.get('MemTotal', 0)) * 1024, int(mem.get('MemAvailable', 0)) * 1024
    return [('up %s' % ago(up), None), ('load %.2f' % os.getloadavg()[0], None),
            ('mem %s/%s' % (human(total - avail), human(total)), None)]


def stat_lan(c):
    hosts = c['config'].get('lan') or {}
    if not hosts:
        return None
    entry = cached(c, 'lan')
    if entry is None or entry.get('key') != c['keys']['lan']:
        return [('pending', 'dim')]
    return [(('✓ ' if up else '✗ ') + name, None if up else 'bad') for name, up in (entry.get('value') or {}).items()]


STATS = {'ros': stat_ros, 'dds': stat_dds, 'host': stat_host, 'hw': stat_hw, 'workspace': stat_workspace,
         'repos': stat_repos, 'nodes': stat_nodes, 'time': stat_time, 'disk': stat_disk,
         'system': stat_system, 'lan': stat_lan}


def collect(env, config, ws, now):
    """The configured rows as (name, segments), starting a refresh of stale probes."""
    ttl, hosts, wanted = config.get('ttl') or {}, config.get('lan') or {}, config.get('stats') or []
    keys = {'repos': str(ws), 'lan': json.dumps(hosts, sort_keys=True),
            'nodes': '%s/%s/%s' % (env.get('ROS_DOMAIN_ID', '0'), env.get('RMW_IMPLEMENTATION', ''),
                                   env.get('ASTRO_DDS_ADDRESS', ''))}
    path = cache_path(env)
    cache = read_cache(path)
    jobs = [job for job in (('repos', keys['repos'], lambda: probe_repos(ws)),
                            ('nodes', keys['nodes'], lambda: probe_nodes(dict(env))),
                            ('lan', keys['lan'], lambda: probe_lan(hosts)))
            if job[0] in wanted and (job[0] != 'lan' or hosts) and (job[0] != 'nodes' or env.get('ROS_DISTRO'))
            and stale(cache, job[0], job[1], float(ttl.get(job[0], 60)), now)]
    if jobs:
        refresh(path, jobs)
    context = {'env': env, 'config': config, 'ws': ws, 'now': now, 'cache': cache, 'keys': keys}
    rows = []
    for name in wanted:
        try:
            segments = STATS[name](context)
        except Exception:  # one broken probe must not cost the whole banner
            segments = None
        if segments:
            rows.append((name, segments))
    return rows
