"""Private Linux publication primitive. No runtime or baseline promotion.

The authoring name is briefly absent. Retained inodes are mutable editor lineage,
never immutable snapshots. Arbitrary future descriptor writes require inspection.
All participating cooperative writers must use this directory's lock.
"""
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager
from ._store import DIRECTORY


@contextmanager
def _directory(path):
    fd = os.open('/', DIRECTORY)
    try:
        for part in filter(None, os.path.abspath(path).split('/')):
            child = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _move(source_fd, source, target_fd, target):
    """Linux renameat2(RENAME_NOREPLACE); never emulate with check + replace."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, 'renameat2', None)
    if rename is None:
        raise OSError(errno.ENOTSUP, 'renameat2 unavailable')
    if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _read(fd, name):
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(opened, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('publication requires regular files')
        data = stream.read()
        os.fsync(stream.fileno())
        return data, {'device': info.st_dev, 'inode': info.st_ino,
                      'digest': hashlib.sha256(data).hexdigest()}


def observe(destination):
    """Preview identity and bytes digest; this observation is not a CAS fence."""
    with _directory(os.path.dirname(os.path.abspath(destination))) as parent:
        try:
            return _read(parent, os.path.basename(destination))[1]
        except FileNotFoundError:
            return None


def _write(fd, name, data):
    opened = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=fd)
    with os.fdopen(opened, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(fd)


def _event(fd, status):
    opened = os.open('events', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                     0o600, dir_fd=fd)
    with os.fdopen(opened, 'ab') as stream:
        stream.write((status + '\n').encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(fd)
    return status


def prepare(destination, candidate, expected):
    """Retain separate immutable candidate bytes and publishable copy, plus intent.

    Returns a sibling spool path. Never remove this spool automatically. Caller
    supplies the preview observation, including None for an absent destination.
    """
    destination = os.path.abspath(destination)
    parent_path, name = os.path.split(destination)
    with _directory(parent_path) as parent:
        spool_name = '.config-publication-' + uuid.uuid4().hex
        os.mkdir(spool_name, 0o700, dir_fd=parent)
        os.fsync(parent)
        spool = os.open(spool_name, DIRECTORY, dir_fd=parent)
        try:
            _write(spool, 'snapshot', candidate)
            _write(spool, 'candidate', candidate)
            _write(spool, 'intent', json.dumps({'name': name, 'expected': expected,
                   'candidate_digest': hashlib.sha256(candidate).hexdigest()}).encode())
            _event(spool, 'prepared')
        finally:
            os.close(spool)
    return os.path.join(parent_path, spool_name)


@contextmanager
def _opened(spool_path):
    with _directory(os.path.dirname(os.path.abspath(spool_path))) as parent:
        spool = os.open(os.path.basename(spool_path), DIRECTORY, dir_fd=parent)
        try:
            intent = json.loads(_read(spool, 'intent')[0])
            name = intent['name']
            if name in ('', '.', '..') or '/' in name:
                raise ValueError('invalid authoring name')
            yield parent, spool, intent
        finally:
            os.close(spool)


def publish(spool_path):
    """One attempt only; interrupted/failed attempts require explicit recovery.

    A conflict may leave the authoring name absent. Never restore over an editor.
    This result is not permission to promote any baseline or evidence manifest.
    """
    with _opened(spool_path) as (parent, spool, intent):
        lock = os.open('.config-publication.lock', os.O_RDWR | os.O_CREAT |
                       os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise ValueError('nonregular publication lock')
            fcntl.flock(lock, fcntl.LOCK_EX)
            if _read(spool, 'events')[0] != b'prepared\n':
                raise ValueError('publication already attempted; inspect/recover')
            _event(spool, 'attempting')
            name, expected = intent['name'], intent['expected']
            if expected is not None:
                # Refuse known links/nonregular objects before moving anything.
                _read(parent, name)
                _move(parent, name, spool, 'displaced')
                os.fsync(parent)
                os.fsync(spool)
                if _read(spool, 'displaced')[1] != expected:
                    return _event(spool, 'conflict_displaced')
            _move(spool, 'candidate', parent, name)
            os.fsync(parent)
            os.fsync(spool)
            published = _read(parent, name)[1]
            _write(spool, 'published', json.dumps(published).encode())
            status = 'published' if published['digest'] == intent['candidate_digest'] else 'conflict_destination'
            if expected is not None and _read(spool, 'displaced')[1] != expected:
                status = 'conflict_displaced'
            return _event(spool, status)
        except (OSError, ValueError):
            _event(spool, 'conflict_or_unsupported')
            raise
        finally:
            os.close(lock)


def inspect(spool_path):
    """Report retained lineage changes, including writes through old open fds."""
    with _opened(spool_path) as (parent, spool, intent):
        result = {'events': _read(spool, 'events')[0].decode().splitlines()}
        for key, fd, name in (('displaced', spool, 'displaced'),
                              ('destination', parent, intent['name'])):
            try:
                result[key] = _read(fd, name)[1]
            except FileNotFoundError:
                result[key] = None
        result['source_changed'] = (result['displaced'] is not None and
                                    result['displaced'] != intent['expected'])
        return result
