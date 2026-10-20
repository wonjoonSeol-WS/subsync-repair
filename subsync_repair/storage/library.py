"""The library folders the tools may work in: which one holds a path, and walking them."""
import os
import sys
from pathlib import Path

from subsync_repair.storage.fileio import is_link

CASE_INSENSITIVE = sys.platform in ("win32", "darwin")      # their default file systems ignore letter case


def lexical_key(path):
    """`path` made absolute, and case-folded where file names ignore case; the file system is not asked."""
    key = os.path.normcase(os.path.abspath(path))
    return key.casefold() if CASE_INSENSITIVE else key


def is_within(path_key, root_key):
    try:
        return os.path.commonpath([path_key, root_key]) == root_key
    except ValueError:      # different drives, or a UNC path against a drive letter
        return False


class Libraries:
    def __init__(self, roots):
        self.roots = tuple(Path(root) for root in roots)
        self.real_keys = tuple(lexical_key(os.path.realpath(root)) for root in self.roots)

    def __iter__(self):
        return iter(self.roots)

    def containing(self, path):
        """The library folder that holds `path`, judged by the text of the path alone (nothing is looked up)."""
        key = lexical_key(path)
        return next((root for root in self.roots if is_within(key, lexical_key(root))), None)

    def resolve(self, path):
        """The real path of the absolute `path` when it lies in a library both as written and after following links,
        else None. Checking the text first keeps a path outside every library away from the file system, where a UNC
        path would make Windows contact its host; links inside a library are followed, so a symlink there to a UNC
        share is contacted (a junction cannot point to one)."""
        if self.containing(path) is None:
            return None
        real = os.path.realpath(path)
        real_key = lexical_key(real)
        return Path(real) if any(is_within(real_key, root_key) for root_key in self.real_keys) else None


def walk(root, skip_dirs=frozenset()):
    """(folder, sorted files) for every folder under `root`, top-down in name order. Folders whose lower-case name is
    in `skip_dirs` are left out, and symlinks and junctions to folders are never entered: they may lead out of the
    library."""
    pending = [Path(root)]
    while pending:
        folder = pending.pop()
        files, folders = entries_of(folder, skip_dirs)
        yield folder, files
        pending.extend(reversed(folders))


def entries_of(folder, skip_dirs):
    """(the files of `folder` as listing() sees them, the folders under it that walk() enters)."""
    files, folders = [], []
    for entry in scan(folder):
        if is_file(entry):
            files.append(Path(entry.path))
            continue
        try:
            if entry.is_dir(follow_symlinks=False) and entry.name.lower() not in skip_dirs and not is_link(entry.path):
                folders.append(Path(entry.path))
        except OSError:
            continue
    return sorted(files), folders


def listing(folder):
    """The files of a folder, sorted, links to files included; an unreadable folder has none."""
    return sorted(Path(entry.path) for entry in scan(folder) if is_file(entry))


def scan(folder):
    """The entries of a folder in name order; an unreadable folder has none."""
    try:
        with os.scandir(folder) as found:
            return sorted(found, key=lambda entry: entry.name)
    except OSError:
        return []


def is_file(entry):
    try:
        return entry.is_file()
    except OSError:
        return False
