"""The verdict cache: a subtitle whose files have not changed since its last verdict is not checked again. Verdicts
are kept only for the settings, tools and code they were reached with (the fingerprint)."""
import functools
import hashlib
import json
import logging
import os
from pathlib import Path

from subsync_repair import __version__, engine, external, formats
from subsync_repair.engine.verdicts import Status, clip
from subsync_repair.external import programs
from subsync_repair.storage.fileio import read_json, write_json
from subsync_repair.storage.library import is_within, lexical_key

FORMAT_VERSION = 3
STATUSES = {s.value for s in Status}
VERDICT_PACKAGES = (engine, external, formats)      # the code a verdict depends on

log = logging.getLogger(__name__)


def verdict_sources():
    """The source files of the code that reaches verdicts."""
    return sorted(path for package in VERDICT_PACKAGES for path in Path(package.__file__).parent.glob("*.py"))


@functools.lru_cache(maxsize=None)
def code_hash():
    """A hash of the code that reaches verdicts, so that an upgrade makes earlier verdicts stale."""
    digest = hashlib.sha256()
    for path in verdict_sources():
        digest.update(path.read_bytes())
    return digest.hexdigest()


def fingerprint(settings):
    """Changes whenever a setting, a tool or the code that a verdict depends on changes."""
    t = settings.tools
    data = {"version": __version__, "code": code_hash(), "embedded": settings.embedded_order,
            "sibling": settings.sibling_order, "alass": programs.identity(t.alass),
            "ffmpeg": programs.identity(t.ffmpeg), "ffprobe": programs.identity(t.ffprobe),
            "ffsubsync": [t.ffsubsync_python, t.ffsubsync_libs]}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class VerdictCache:
    """Entries {"sig", "status", "note"} by the lexical key of a subtitle's real path."""

    def __init__(self, path, fingerprint):
        self.path, self.fingerprint = Path(path), fingerprint
        self.files = {}

    def load(self):
        data = read_json(self.path, "verdict cache")
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            return self
        if data.get("version") == FORMAT_VERSION and data.get("settings") == self.fingerprint:
            self.files = {key: entry for key, entry in data["files"].items() if isinstance(entry, dict)}
        return self

    def get(self, real, sig):
        """The entry for the subtitle at the real path `real` while its files still have the signature `sig`."""
        entry = self.files.get(lexical_key(real))
        if not entry or entry.get("sig") != sig or entry.get("status") not in STATUSES:
            return None
        return entry

    def put(self, real, sig, verdict):
        self.files[lexical_key(real)] = {"sig": sig, "status": verdict.status.value, "note": clip(verdict.note)}

    def prune(self, root, seen, keep):
        """Forget entries under the library folder `root` (followed to its real path, like the entries) that a full
        scan of it did not see (`seen`: lexical keys) unless `keep(key)` says the subtitle is still there to repair, as
        a hook's verdict on another language is."""
        root_key = lexical_key(os.path.realpath(root))
        for key in [k for k in self.files if k not in seen and is_within(k, root_key)]:
            if not keep(key):
                del self.files[key]

    def save(self):
        """Write the cache; a failure is logged and the run goes on, at worst checking files again next time."""
        try:
            write_json(self.path, {"version": FORMAT_VERSION, "settings": self.fingerprint, "files": self.files})
            return True
        except OSError as e:
            log.warning("could not save the verdict cache %s: %s", self.path, e)
            return False
