"""Small private state primitives; callers use administrator-owned directories."""
import json
import os
from pathlib import Path
import stat
import fcntl
from contextlib import contextmanager
import tempfile


@contextmanager
def log_lock(directory):
    fd = os.open(directory / '.log-io.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError('Invalid log lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def directory(path):
    path = Path(path)
    if path.is_symlink():
        raise OSError('Unsafe state directory')
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise OSError('State directory must be private and owned by service user')
    return path


def read(path, limit=1048576):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('Invalid state file')
        return json.loads(stream.read(limit + 1))


def atomic(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)
