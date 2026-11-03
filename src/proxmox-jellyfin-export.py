#!/usr/bin/env python3
"""Copy bounded Jellyfin log tails from an LXC root into a private host snapshot."""
__version__ = "0.2.0"
import datetime as dt
import os
from pathlib import Path
import stat
import tempfile

CONTAINER = os.environ.get("PRW_JELLYFIN_CONTAINER_ID", "")
DEST = Path(os.environ.get("PRW_JELLYFIN_SNAPSHOT", "/var/log/proxmox-resource-watch/jellyfin-context.log"))
LIMIT = 512 * 1024


def container_init_pid(container):
    """Resolve the systemd LXC init via read-only cgroup-v2 metadata."""
    procs = Path(f"/sys/fs/cgroup/lxc/{container}/ns/init.scope/cgroup.procs")
    with procs.open("r", encoding="ascii") as stream:
        candidates = stream.read(8192).splitlines()
    found = []
    for candidate in candidates:
        if not candidate.isascii() or not candidate.isdecimal() or int(candidate) < 1:
            continue
        try:
            with open(f"/proc/{candidate}/status", encoding="ascii") as stream:
                status = stream.read(65536)
            with open(f"/proc/{candidate}/cgroup", encoding="ascii") as stream:
                cgroups = stream.read(8192).splitlines()
        except OSError:
            continue
        nspid = next((line.split()[1:] for line in status.splitlines() if line.startswith("NSpid:")), [])
        if nspid and nspid[-1] == "1" and f"0::/lxc/{container}/ns/init.scope" in cgroups:
            found.append(candidate)
    if len(found) != 1:
        raise OSError("Cannot identify a unique running container init")
    return found[0]


def export(container=CONTAINER, destination=DEST, today=None):
    if not container.isascii() or not container.isdecimal() or int(container) < 1:
        raise ValueError("Invalid container ID")
    pid = container_init_pid(container)
    today = today or dt.datetime.now().astimezone().date()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    root = os.open(f"/proc/{pid}/root", flags)
    fds = [root]
    try:
        parent = root
        for component in ("var", "log", "jellyfin"):
            parent = os.open(component, flags | os.O_NOFOLLOW, dir_fd=parent)
            fds.append(parent)
        chunks = []
        for day in (today-dt.timedelta(days=1), today):
            streams = []
            try:
                # Jellyfin may leave an empty dated file and rotate its data to .1.
                for suffix in (".log.1", ".log"):
                    name = f"jellyfin{day:%Y%m%d}{suffix}"
                    try:
                        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
                    except FileNotFoundError:
                        continue
                    stream = os.fdopen(fd, "rb")
                    streams.append(stream)
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError("Jellyfin log is not a regular file")
                nonempty = [stream for stream in streams if os.fstat(stream.fileno()).st_size]
                if not nonempty:
                    print(f"Jellyfin log missing or empty for {day}")
                for stream in nonempty:
                    size = os.fstat(stream.fileno()).st_size
                    limit = LIMIT // len(nonempty)
                    offset = max(0, size-limit)
                    stream.seek(offset)
                    data = stream.read(limit)
                    if offset:
                        data = data.partition(b"\n")[2]
                    if data:
                        chunks.append(data.rstrip(b"\n") + b"\n")
            finally:
                for stream in streams:
                    stream.close()
        if not chunks:
            raise OSError("No Jellyfin log content available")
    finally:
        for fd in reversed(fds):
            os.close(fd)
    directory = destination.parent
    info = directory.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise OSError("Snapshot directory must be root-owned and private")
    fd, name = tempfile.mkstemp(prefix=".jellyfin-context-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o400)
            for chunk in chunks:
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, destination)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print(f"Jellyfin snapshot updated ({sum(map(len, chunks))} bytes)")


if __name__ == "__main__":
    export()
