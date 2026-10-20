"""Which subtitles a repair run checks, and checking each with the help of the verdict cache."""
import os
from dataclasses import dataclass, replace
from pathlib import Path

from subsync_repair.engine.repairer import repairable, verdict_key
from subsync_repair.engine.verdicts import Status, Verdict, file_error
from subsync_repair.formats.names import SUBTITLE_EXT
from subsync_repair.storage.library import lexical_key, listing, walk

SAVE_EVERY = 20             # verdicts stored between cache saves, so an interrupted scan keeps its progress
CHANGES_FILES = (Status.FIXED, Status.ERROR)    # a repair, or a failed one that could not be undone, changed files
OUTSIDE = "not in a configured library"


@dataclass(frozen=True)
class Target:
    path: Path              # the subtitle as the run reaches it
    real: Path              # its real path, which keys the verdict cache
    entries: tuple          # the files of its folder
    shown: str              # how output lines name it


@dataclass(frozen=True)
class Result:
    target: Target
    verdict: Verdict
    cached: bool = False


def library_targets(settings, root):
    """Every subtitle in the target language under the library folder `root` that a scan repairs."""
    for _, files in walk(root, settings.skip_dirs):
        entries = tuple(files)
        for path in files:
            if path.suffix.lower() not in SUBTITLE_EXT:
                continue
            name = repairable(path, entries)
            if not name or name.lang != settings.target:
                continue
            real = settings.libraries.resolve(str(path))
            if real is not None:
                yield Target(path, real, entries, str(path.relative_to(root)))


def explicit_targets(raw_paths, libraries):
    """(targets, refused results) for paths named on the command line: each must be a file inside a configured
    library, and a file named twice is checked once."""
    targets, refused, seen = [], [], set()
    for raw in raw_paths:
        path = Path(raw).absolute()
        real = libraries.resolve(str(path))
        if real is None:
            refused.append(refusal(path, Status.SKIP, OUTSIDE))
        elif real.is_dir():
            refused.append(refusal(path, Status.ERROR, "not a file"))
        elif not real.is_file():
            refused.append(refusal(path, Status.ERROR, "no such file"))
        elif lexical_key(real) not in seen:
            seen.add(lexical_key(real))
            targets.append(Target(real, real, tuple(listing(real.parent)), str(path)))
    return targets, refused


def refusal(path, status, note):
    return Result(Target(path, path, (), str(path)), Verdict(status, note, cacheable=False))


class Checker:
    """Checks targets, answering from the verdict cache while a subtitle's files stay the same. With `store`, new
    verdicts go into the cache, which is saved after every SAVE_EVERY of them. A folder whose files a check changed is
    listed again for the next target in it."""

    def __init__(self, cache, use_cached=True, store=False):
        self.cache, self.use_cached, self.store = cache, use_cached, store
        self.stored = 0
        self.changed = set()            # lexical keys of the folders whose files a check changed

    def check(self, target, act):
        """The Result for `target`: the cached verdict, or `act(target)`."""
        folder = lexical_key(target.path.parent)
        if folder in self.changed:
            target = replace(target, entries=tuple(listing(target.path.parent)))
        try:
            key = verdict_key(target.path, target.entries)
        except OSError as e:
            return Result(target, file_error(e))
        hit = self.cache.get(target.real, key) if self.use_cached else None
        if hit:
            return Result(target, Verdict(Status(hit["status"]), hit["note"]), cached=True)
        verdict = act(target)
        if verdict.status in CHANGES_FILES:
            self.changed.add(folder)
        if self.store and verdict.cacheable:
            self.remember(target, key, verdict)
        return Result(target, verdict)

    def remember(self, target, key, verdict):
        """Store `verdict` under the files as they are now: a check that changed them stores it under the new ones, and
        a converted .smi is gone in favour of its .srt."""
        real = target.real
        if verdict.status in CHANGES_FILES:
            written = Path(verdict.output or target.path)
            try:
                key, real = verdict_key(written, listing(written.parent)), Path(os.path.realpath(written))
            except OSError:
                return              # gone or unreadable now: it is simply checked again next time
        self.cache.put(real, key, verdict)
        self.stored += 1
        if self.stored % SAVE_EVERY == 0:
            self.cache.save()

    def prune(self, root, seen):
        """Forget the cache entries under the library folder `root` that a full scan of it did not see (`seen`), unless
        the subtitle is still there to repair, as a hook's verdict on another language is."""
        listings = {}

        def still_repairable(key):
            path = Path(key)
            if not path.is_file():
                return False
            if path.parent not in listings:
                listings[path.parent] = listing(path.parent)
            return repairable(path, listings[path.parent]) is not None

        self.cache.prune(root, seen, still_repairable)

    def save(self):
        self.cache.save()
