"""Finding the external programs, and the error a program that cannot do its job raises."""
import os
import shutil
from pathlib import Path

from subsync_repair.storage.fileio import signature


class ToolError(Exception):
    """A tool could not do its job. `message` is fit to show anyone; `detail` (paths, tool output) is for the log."""

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.message, self.detail = message, detail


def locate(command):
    """The file `command` runs: an absolute path (on Windows also with an extension from PATHEXT), or a program on
    PATH. None when there is none."""
    path = Path(command)
    if not path.is_absolute():
        found = shutil.which(str(command))
        return Path(found) if found else None
    extensions = os.environ.get("PATHEXT", ".EXE").split(os.pathsep) if os.name == "nt" else []
    candidates = [path] + [Path(str(path) + ext) for ext in extensions if ext]
    return next((p for p in candidates if p.is_file()), None)


def available(command):
    return locate(command) is not None


def identity(command):
    """Which file `command` runs and which build of it (size and modification time), for the verdict cache."""
    path = locate(command)
    if path is None:
        return f"{command}: missing"
    real = os.path.realpath(path)
    try:
        return f"{real}: {signature(real)}"
    except OSError:
        return f"{real}: unreadable"
