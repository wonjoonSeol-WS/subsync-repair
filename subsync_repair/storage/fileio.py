"""Crash-safe file writes (a half-written subtitle never replaces a good one) and a lock between processes."""
import contextlib
import errno
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

TEMP_PREFIX = ".ssr-"
TEMP_SUFFIX = ".tmp"
TEMP_NAME = re.compile(re.escape(TEMP_PREFIX) + "[0-9a-f]{8}" + re.escape(TEMP_SUFFIX))
TEMP_ATTEMPTS = 100
CLAIM = b"subsync-repair: unfinished write\n"    # what a new name holds until its content is moved in
STALE_AFTER = 24 * 3600     # seconds after which the leftovers of an interrupted write are removed
CREATE_MODE = 0o666         # reduced by the umask
LOCK_POLL = 1.0             # seconds between attempts to take a lock
NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
BINARY = getattr(os, "O_BINARY", 0)

log = logging.getLogger(__name__)
_swept = set()
_swept_lock = threading.Lock()


def atomic_write_bytes(path, data, exclusive=False, follow_links=True, like=None):
    """Write `data` so that a reader sees either the old file or the whole new one. A read-only file is refused
    (PermissionError). With `exclusive=True` an existing file is never replaced (FileExistsError). A link at `path` is
    written through to its target, or refused with `follow_links=False`. A replaced file keeps its mode and owner; a
    new one takes those of the file `like` (a converted subtitle), else the umask decides."""
    path = Path(os.path.realpath(path) if follow_links else os.path.abspath(path))
    if not follow_links and is_link(path):
        raise PermissionError(errno.EPERM, "the destination is a link", str(path))
    current = stat_or_none(path)
    if current is not None and not exclusive and not current.st_mode & stat.S_IWUSR:
        raise PermissionError(errno.EACCES, "the file is read-only", str(path))
    template = current if current is not None else (stat_or_none(like) if like is not None else None)
    fd, tmp = create_temp(path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            publish_new(tmp, path)
        else:
            if template is not None:
                copy_mode_and_owner(tmp, template)
            os.replace(tmp, path)
    finally:
        discard(tmp)
    if exclusive and template is not None:      # after the temporary name is gone: a hard link shares the mode
        copy_mode_and_owner(path, template)
    sync_folder(path.parent)


def create_temp(folder):
    """(descriptor, path) of a new empty file in `folder` whose short name marks it as this tool's temporary file."""
    for _ in range(TEMP_ATTEMPTS):
        path = Path(folder) / f"{TEMP_PREFIX}{secrets.token_hex(4)}{TEMP_SUFFIX}"
        try:
            return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | BINARY | NO_FOLLOW, CREATE_MODE), path
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "no free temporary name", str(folder))


def copy_mode_and_owner(path, template):
    os.chmod(path, stat.S_IMODE(template.st_mode))
    if os.name != "nt":
        with contextlib.suppress(OSError):      # only root may give a file away, and a group needs membership
            os.chown(path, template.st_uid if os.geteuid() == 0 else -1, template.st_gid)


def publish_new(tmp, path):
    """Give `tmp` the name `path`, which must not exist yet."""
    try:
        os.link(tmp, path)
        return
    except FileExistsError:
        raise
    except OSError:
        pass                                    # no hard links here (FAT, exFAT, some network shares)
    if os.name == "nt":
        os.rename(tmp, path)                    # never replaces an existing file on Windows
    else:
        claim_and_replace(tmp, path)


def claim_and_replace(tmp, path):
    """Take the free name `path` with a recognisable claim, then move `tmp` over it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | BINARY | NO_FOLLOW, CREATE_MODE)
    try:
        os.write(fd, CLAIM)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        discard(Path(path))
        raise


def sweep_stale(folder):
    """Remove what interrupted writes of this tool left in `folder` a day or more ago: temporary files and claimed
    names. A folder is swept once per process."""
    key = os.path.normcase(os.path.abspath(folder))
    with _swept_lock:
        if key in _swept:
            return
        _swept.add(key)
    cutoff = time.time() - STALE_AFTER
    try:
        with os.scandir(folder) as found:
            entries = list(found)
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            st = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(st.st_mode) and st.st_mtime < cutoff and is_leftover(entry.name, entry.path, st):
                os.unlink(entry.path)
                log.info("removed %s, left by an interrupted write", entry.path)


def is_leftover(name, path, st):
    if TEMP_NAME.fullmatch(name):
        return True
    if st.st_size != len(CLAIM):
        return False
    with open(path, "rb") as f:
        return f.read() == CLAIM


def is_link(path):
    """A symlink, or a Windows junction, which os.path.islink does not report."""
    if os.path.islink(path):
        return True
    if hasattr(os.path, "isjunction"):          # Python 3.12 and later
        return os.path.isjunction(path)
    try:
        return os.lstat(path).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except (OSError, AttributeError):
        return False


def stat_or_none(path):
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None


def signature(path):
    """The version of a file: its size and modification time."""
    st = os.stat(path)
    return f"{st.st_size}:{st.st_mtime_ns}"


def file_key(path):
    """A key that changes whenever the file at `path` does."""
    return str(path), signature(path)


def read_json(path, what):
    """The JSON value in the file `path`, or None when there is no such file. An unreadable one is logged as the
    unreadable `what` and also gives None."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("ignoring the unreadable %s %s: %s", what, path, e)
        return None


def write_json(path, data):
    """Write `data` to `path` as JSON with atomic_write_bytes, making its folder first."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps(data, indent=1, ensure_ascii=False).encode("utf-8"))


def remove_file(path):
    """Remove a file this tool made, also when it took a read-only mode from the file it was made like."""
    try:
        os.unlink(path)
    except PermissionError:
        make_writable(path)
        os.unlink(path)


def make_writable(path):
    """Add the owner's write permission, which on Windows clears the read-only flag that keeps a file from being
    removed. The other permissions stay: a temporary file may be a hard link to the file just written."""
    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IWRITE)


def discard(tmp):
    """Remove a temporary file; failing to is logged, never raised, so it cannot hide how the write went."""
    for attempt in range(2):
        try:
            tmp.unlink(missing_ok=True)
            return
        except OSError as e:
            if attempt:
                log.warning("could not remove the temporary file %s: %s", tmp, e)
                return
            with contextlib.suppress(OSError):
                make_writable(tmp)


def sync_folder(folder):
    """Make a rename in `folder` durable (POSIX; Windows has no directory handles to flush)."""
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def process_lock(path, wait=0.0, poll=None):
    """Held while the block runs. Waits up to `wait` seconds for another process to let go, then TimeoutError. The
    operating system releases the lock when its process ends, so a crash never leaves a stale lock."""
    poll = LOCK_POLL if poll is None else poll
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    with open(path, "a+b") as f:
        while True:
            try:
                lock_file(f)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"another run held {path} for {wait:g} s") from None
                time.sleep(poll)
        try:
            yield
        finally:
            unlock_file(f)


if os.name == "nt":
    import msvcrt

    def lock_file(f):
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise BlockingIOError(errno.EAGAIN, "locked by another process", f.name) from None

    def unlock_file(f):
        f.seek(0)
        with contextlib.suppress(OSError):
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def lock_file(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock_file(f):
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
