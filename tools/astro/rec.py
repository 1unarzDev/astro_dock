"""`astro rec start [preset] [label] | stop | ls | fetch [BAG]`: ros2 bag record (mcap, 300 s splits) into
<data>/<date>/bags/<HHMMSS>[-label]/, its pid in <data>/<date>/.rec.pid; ls shows each bag's span and the
log lines inside it; fetch copies one of the home host's bags here (rsync through `astro on`)."""
import datetime
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time


from . import config
from .engine import log


def presets(vessel):
    data = config.load_yaml(vessel.path('bags')) or {}
    data = data.get('presets', data) if isinstance(data, dict) else {}
    if not isinstance(data, dict) or not data:
        raise config.Error(f'{vessel.path("bags")}: expected presets, NAME: [topics] or NAME: all')
    return data


def pid_file(root):
    return log.day_dir(root) / '.rec.pid'


def running(root):
    path = pid_file(root)
    if not path.exists():
        return None
    pid, _, directory = path.read_text().partition(' ')
    return (int(pid), directory.strip()) if log.pid_alive(pid) else None


def start(vessel, words):
    root = vessel.data_root()
    if running(root):
        raise config.Error(f'already recording {running(root)[1]} (astro rec stop)')
    table = presets(vessel)
    preset = words[0] if words and words[0] in table else next(iter(table))
    label = '-'.join(words[1:] if words and words[0] in table else words)
    topics = table[preset]
    now = datetime.datetime.now()
    name = f'{now:%H%M%S}' + (f'-{label}' if label else '')
    bags = log.day_dir(root) / 'bags'
    bags.mkdir(parents=True, exist_ok=True)
    out = bags / name
    command = ['ros2', 'bag', 'record', '-s', 'mcap', '-d', '300', '-o', str(out)]
    if topics == 'all':
        command.append('--all-topics')
        exclude = (config.load_yaml(vessel.path('bags')) or {}).get('exclude')
        if exclude:
            command += ['--exclude-regex', str(exclude)]
    else:
        topics = list(topics)
        events = vessel.astro.get('events')
        if events and events not in topics:
            topics.append(events)
        command += topics
    with open(bags / f'{name}.log', 'w') as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    time.sleep(1.0)
    if process.poll() is not None:
        raise config.Error(f'ros2 bag record exited: see {bags / (name + ".log")}')
    pid_file(root).write_text(f'{process.pid} {out}')
    # The name was fixed before the recorder started (-o), so the line names the bag; it waits until the
    # recorder has subscribed, so the line is in the bag too.
    log.write(root, 'rec', f'start {out.relative_to(log.day_dir(root))} ({preset})', vessel.astro.get('events'),
              wait=True)
    return 0


def stop(vessel):
    root = vessel.data_root()
    found = running(root)
    if not found:
        print('not recording')
        pid_file(root).unlink(missing_ok=True)
        return 0
    pid, directory = found
    log.write(root, 'rec', f'stop  bags/{Path(directory).name}', vessel.astro.get('events'), wait=True)
    os.killpg(pid, signal.SIGINT)
    for _ in range(100):
        if not log.pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        os.killpg(pid, signal.SIGTERM)
    pid_file(root).unlink(missing_ok=True)
    return 0


def span(directory):
    """(start, end) epoch seconds from a bag's metadata.yaml, or None while it records."""
    meta = config.load_yaml(Path(directory) / 'metadata.yaml')
    info = (meta or {}).get('rosbag2_bagfile_information') if isinstance(meta, dict) else None
    if not info:
        return None
    start = info['starting_time']['nanoseconds_since_epoch'] / 1e9
    return start, start + info['duration']['nanoseconds'] / 1e9


def ls(vessel, day=None):
    """Each bag with the log lines inside it, compared on epoch seconds (the log's clock is its .tz)."""
    root = vessel.data_root()
    day = day or datetime.date.today()
    lines = [line for line in map(log.parse_line, log.read(root, day)) if line]
    stamped = [(log.epoch(root, day, stamp), stamp, kind, message) for stamp, kind, message in lines]
    bags = sorted(p for p in (log.day_dir(root, day) / 'bags').glob('*') if p.is_dir())
    if not bags:
        print(f'no bags in {log.day_dir(root, day) / "bags"}')
    for bag in bags:
        found = span(bag)
        if found:
            low, high = found
            head = (f'{datetime.datetime.fromtimestamp(low):%H:%M:%S}-'
                    f'{datetime.datetime.fromtimestamp(high):%H:%M:%S}')
        else:
            starts = [e for e, _, kind, message in stamped if kind == 'rec' and bag.name in message]
            low, high = (starts[-1] if starts else 0.0), time.time()
            head = f'{datetime.datetime.fromtimestamp(low):%H:%M:%S}-(recording)' if starts else '(recording)'
        print(f'{head}  {bag.relative_to(root)}')
        for when, stamp, kind, message in stamped:
            if low - 1 <= when <= high + 1:
                print(f'    {stamp}  {kind:<{log.WIDTH}} {message}')
    return 0


# ---- fetch: a bag of the home host, copied here --------------------------------------------------------

# Run on the home host as `python3 - DATA`: one tab-separated line per bag of every day,
# astro-bag DATE NAME PATH BYTES START END (epoch seconds from metadata.yaml, - while it records).
LIST = r"""
import os, re, sys
root = sys.argv[1]
days = sorted(os.listdir(root)) if os.path.isdir(root) else []
for day in days:
    bags = os.path.join(root, day, 'bags')
    if not re.match(r'\d{4}-\d\d-\d\d$', day) or not os.path.isdir(bags):
        continue
    for name in sorted(os.listdir(bags)):
        path = os.path.join(bags, name)
        if not os.path.isdir(path):
            continue
        size = 0
        for base, _, files in os.walk(path):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
        start = end = '-'
        try:
            text = open(os.path.join(path, 'metadata.yaml')).read()
            t0 = re.search(r'starting_time:\s*nanoseconds_since_epoch:\s*(\d+)', text)
            dt = re.search(r'duration:\s*nanoseconds:\s*(\d+)', text)
            if t0 and dt:
                start, end = '%.0f' % (int(t0.group(1)) / 1e9), '%.0f' % ((int(t0.group(1)) + int(dt.group(1))) / 1e9)
        except OSError:
            pass
        print('astro-bag', day, name, path, size, start, end, sep='\t')
"""


def size_text(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024


def parse_bags(lines):
    """[{date, name, path, size, start, end}] from the LIST lines, newest first."""
    out = []
    for line in lines:
        fields = line.split('\t')
        if len(fields) == 7 and fields[0] == 'astro-bag':
            _, date, name, path, size, start, end = fields
            out.append({'date': date, 'name': name, 'path': path, 'size': int(size) if size.isdigit() else 0,
                        'start': float(start) if start != '-' else None, 'end': float(end) if end != '-' else None})
    return sorted(out, key=lambda b: (b['date'], b['name']), reverse=True)


def choice(bag):
    """(value, about) of a bag for the picker: DATE/NAME, its label, size and start-end."""
    label = bag['name'].partition('-')[2] or '-'
    span = '(recording or no metadata)'
    if bag['start'] is not None:
        span = (f'{datetime.datetime.fromtimestamp(bag["start"]):%H:%M:%S}-'
                f'{datetime.datetime.fromtimestamp(bag["end"]):%H:%M:%S}')
    return f'{bag["date"]}/{bag["name"]}', f'{label:<16} {size_text(bag["size"]):>9}  {span}'


def remote_bags(vessel, home):
    """The home host's bags (parse_bags), listed in its dev container."""
    from . import remote, stacks
    host, spec = remote.place(vessel, home)
    argv, env = remote.words(vessel, host, spec, f'd={stacks.data_expr(vessel.astro)}; python3 - "$d"')
    done = subprocess.run(argv, env=env, input=LIST, capture_output=True, text=True)
    if done.returncode != 0 and 'astro-bag' not in done.stdout:
        raise config.Error(f'could not list the bags on {host}: {(done.stderr.strip().splitlines() or ["no answer"])[-1]}')
    return parse_bags(done.stdout.splitlines())


def find(bags, wanted):
    """The bag named DATE/NAME, else the newest named NAME, else the one whose DATE/NAME contains WANTED."""
    for bag in bags:
        if wanted in (f'{bag["date"]}/{bag["name"]}', bag['name']):
            return bag
    found = [b for b in bags if wanted in f'{b["date"]}/{b["name"]}']
    if len(found) == 1:
        return found[0]
    if found:
        raise config.Error(f'{wanted!r} matches {len(found)} bags: '
                           + ', '.join(f'{b["date"]}/{b["name"]}' for b in found[:6]))
    raise config.Error(f'no bag {wanted!r} on the home host (astro rec fetch lists them)')


def rsh():
    """The rsync remote shell: astro's own _rsh, which runs rsync's server in the host's dev container."""
    return shlex.join([sys.executable, str(config.WS / 'tools' / 'bin' / 'astro'), '_rsh'])


def fetch_argv(host, path, dest, shell):
    """rsync of the bag directory PATH on HOST into DEST (resumable; progress on a terminal)."""
    return ['rsync', '-a', '--partial', '--info=progress2', '-e', shell, f'{host}:{path.rstrip("/")}/',
            f'{str(dest).rstrip("/")}/']


def fetch(vessel, home, wanted, pick):
    """Copy one of HOME's bags into this machine's DATA/DATE/bags/NAME; PICK(title, choices) chooses when
    WANTED is empty."""
    from . import remote
    host, _ = remote.place(vessel, home)
    bags = remote_bags(vessel, home)
    if not bags:
        print(f'no bags on {host}')
        return 0
    if wanted:
        bag = find(bags, wanted)
    else:
        value = pick(f'bag on {host}', [choice(b) for b in bags])
        if value is None:
            return 2
        bag = find(bags, value)
    if not shutil.which('rsync'):
        raise config.Error('rsync is not installed here (apt install rsync)')
    dest = vessel.data_root() / bag['date'] / 'bags' / bag['name']
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f'{host}:{bag["path"]} ({size_text(bag["size"])}) -> {dest}')
    code = subprocess.call(fetch_argv(host, bag['path'], dest, rsh()))
    if code == 0:
        print(f'fetched {dest}')
    return code


def rsh_main(vessel, args):
    """rsync's remote shell: `[-l USER] HOST COMMAND...` runs COMMAND in HOST's dev container."""
    from . import remote
    args = list(args)
    while args and args[0].startswith('-'):        # rsync may pass -l USER; the registry names the user
        args = args[2:] if args[0] == '-l' else args[1:]
    host, spec = remote.place(vessel, args[0])
    argv, env = remote.words(vessel, host, spec, ' '.join(args[1:]))
    if env is not None:
        os.execvpe(argv[0], argv, env)
    os.execvp(argv[0], argv)
