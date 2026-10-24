"""Linux directory-descriptor anchored bootstrap storage; no followed links."""
import json
import os
import stat
from .model import _identity

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class Store:
    def __init__(self, root, scope):
        _identity(scope)
        # Anchor every root component too; a caller-selected root must be a
        # physical directory, not a redirect through a symlink.
        parts = os.path.abspath(root).split(os.sep)
        fd = os.open(os.sep, DIRECTORY)
        try:
            for part in (*filter(None, parts), scope):
                try:
                    os.mkdir(part, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                child = os.open(part, DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def __enter__(self):
        return self

    def __exit__(self, *args):
        os.close(self.fd)

    def _parent(self, relative):
        parts = relative.split('/')
        for part in parts:
            _identity(part)
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def exists(self, relative):
        parent, name = self._parent(relative)
        try:
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('nonregular bootstrap marker')
            return True
        finally:
            os.close(parent)

    def mkdir(self, name):
        _identity(name)
        os.mkdir(name, dir_fd=self.fd)
        os.fsync(self.fd)

    def read(self, relative):
        parent, name = self._parent(relative)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd) as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('nonregular bootstrap object')
                return json.load(stream)
        finally:
            os.close(parent)

    def write(self, relative, data):
        parent, name = self._parent(relative)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)
        finally:
            os.close(parent)
