"""Subtitles the post-processing hook could not repair at once because another run held the lock: one small file each
in <data_dir>/queue, repaired by whichever run holds the lock next."""
import logging
import os
import secrets
import time
from pathlib import Path

from subsync_repair.storage.fileio import atomic_write_bytes, sweep_stale

ENTRY_SUFFIX = ".path"

log = logging.getLogger(__name__)


class HookQueue:
    def __init__(self, folder):
        self.folder = Path(folder)

    def add(self, path):
        self.folder.mkdir(parents=True, exist_ok=True)
        sweep_stale(self.folder)
        entry = self.folder / f"{time.time_ns():020d}-{secrets.token_hex(4)}{ENTRY_SUFFIX}"
        atomic_write_bytes(entry, str(path).encode("utf-8"), exclusive=True)

    def entries(self):
        """[(entry file, queued path)], oldest first."""
        try:
            files = sorted(p for p in self.folder.iterdir() if p.name.endswith(ENTRY_SUFFIX))
        except OSError:
            return []
        found = []
        for entry in files:
            try:
                found.append((entry, entry.read_bytes().decode("utf-8")))
            except OSError as e:
                log.warning("cannot read the queued entry %s: %s", entry, e)
            except UnicodeDecodeError:
                log.warning("dropping the queued entry %s: it is not this tool's", entry)
                self.done(entry)
        return found

    def done(self, entry):
        try:
            os.unlink(entry)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("cannot remove the queued entry %s: %s", entry, e)
