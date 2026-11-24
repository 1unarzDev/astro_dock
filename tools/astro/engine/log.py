"""The day's log: `HH:MM:SS  <kind>  <message>` lines in <data>/<YYYY-MM-DD>/log.txt, each also published on
the events topic (std_msgs/String) by a background process, so writing never waits for ROS."""
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

KINDS = ('mark', 'rec', 'test', 'apply', 'stack', 'rtk', 'trial', 'note')
WIDTH = 7                      # the kind column: 'HH:MM:SS  apply   ...'
LINE = re.compile(r'^(\d\d:\d\d:\d\d)  (\S+) +(.*)$')


def format_line(kind, message, when=None):
    when = when or datetime.datetime.now()
    return f'{when:%H:%M:%S}  {kind:<{WIDTH}} {message}'


def parse_line(line):
    """(time 'HH:MM:SS', kind, message), or None for a line that is not one."""
    match = LINE.match(line.rstrip('\n'))
    return match.groups() if match else None


def seconds(stamp):
    h, m, s = map(int, stamp.split(':'))
    return 3600 * h + 60 * m + s


def day_dir(root, day=None):
    return Path(root) / (day or datetime.date.today()).isoformat()


def publish(topic, line):
    """`ros2 topic pub` the line once in the background (waits for one subscriber, at most 8 s)."""
    if not topic or not shutil.which('ros2'):
        return None
    return subprocess.Popen(['timeout', '8', 'ros2', 'topic', 'pub', '--once', '-w', '1', topic,
                             'std_msgs/msg/String', json.dumps({'data': line})],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def write(root, kind, message, topic=None, echo=True, wait=False):
    """Append one line to today's log, publish it (wait: until it went out), and print it."""
    line = format_line(kind, message)
    directory = day_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / '.tz').exists():          # the clock of the HH:MM:SS lines (see epoch)
        (directory / '.tz').write_text(time.strftime('%z') + '\n')
    with open(directory / 'log.txt', 'a') as out:
        out.write(line + '\n')
    process = publish(topic, line)
    if wait and process:
        process.wait()
    if echo:
        print(line, flush=True)
    return line


def read(root, day=None):
    path = day_dir(root, day) / 'log.txt'
    return path.read_text().splitlines() if path.exists() else []


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def epoch(root, day, stamp):
    """Epoch seconds of a log line's HH:MM:SS on that day, in the clock the day's log was written in
    (its .tz offset; else this machine's)."""
    try:
        offset = (Path(day_dir(root, day)) / '.tz').read_text().strip()
        zone = datetime.datetime.strptime(offset, '%z').tzinfo
    except (OSError, ValueError):
        zone = None
    when = datetime.datetime.combine(day or datetime.date.today(), datetime.time(*map(int, stamp.split(':'))))
    return (when.replace(tzinfo=zone) if zone else when).timestamp()


def posix_tz(path='/etc/localtime'):
    """This machine's zone as a POSIX TZ string (the TZif footer), so a container without tzdata keeps
    the host's clock; '' when unknown."""
    if os.environ.get('TZ'):
        return os.environ['TZ']
    try:
        lines = Path(path).read_bytes().rstrip(b'\n').split(b'\n')
        return lines[-1].decode() if len(lines) > 1 else ''
    except (OSError, UnicodeDecodeError):
        return ''
