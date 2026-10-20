"""subsync-repair: repairs subtitles whose timing drifts in pieces (another offset after an opening, a commercial break
or a different cut) by aligning them with alass to a reference in another language: a track inside the video, or a
subtitle file next to it that ffsubsync has checked against the audio.

  subsync-repair.py                               dry run over the libraries (subtitles in target_language)
  subsync-repair.py --run                         repair them in place; every original is backed up first
  subsync-repair.py [--run] SUBTITLE ...          only these files, in any language, one line of output each
  subsync-repair.py --hook --log FILE SUBTITLE    Bazarr's post-processing hook (see below)
  subsync-repair.py --json SUBTITLE               check one subtitle and print the result as JSON; nothing is written
  subsync-repair.py --json --out NEW.srt SUBTITLE the same, and write the corrected subtitle to a new .srt file
  subsync-repair.py serve [--port PORT] [--log FILE]   the local HTTP server for the browser extension

With --hook (which implies --run) the command acts as Bazarr's hook: it waits at most 30 s for another run, then
queues the file for that run and prints "queued: ...", and it never converts or removes the file it was given.
"""
import argparse
import contextlib
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from subsync_repair.config import DEFAULT_CONFIG, ConfigError, load_settings
from subsync_repair.engine.batch import Checker, Result, explicit_targets, library_targets
from subsync_repair.engine.cache import VerdictCache, fingerprint
from subsync_repair.engine.repairer import Repairer
from subsync_repair.engine.verdicts import Status, Verdict
from subsync_repair.engine.writers import Backups
from subsync_repair.report import Report, TimestampFormatter, file_line, scan_line, summary_line
from subsync_repair.server import create_server
from subsync_repair.storage import datadir
from subsync_repair.storage.datadir import DataDirError
from subsync_repair.storage.fileio import process_lock
from subsync_repair.storage.hook_queue import HookQueue
from subsync_repair.storage.library import lexical_key

LOCK_WAIT = 3600            # seconds a run waits for another one to finish
HOOK_LOCK_WAIT = 30         # the hook runs inside Bazarr's download job, so it queues its file instead of waiting long
CACHE_FILE = "verdict-cache.json"
LOCK_FILE = "subsync-repair.lock"
QUEUE_DIR = "queue"
BACKUP_DIR = "backup"
SERVE = "serve"
QUEUED = "another repair is running; it repairs this file before it ends"
BUSY = "another repair is still running"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
WINDOWS = os.name == "nt"

log = logging.getLogger(__name__)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == [SERVE]:
        return serve_command(argv[1:])
    return repair_command(argv)


def repair_parser():
    ap = argparse.ArgumentParser(prog="subsync-repair.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="subtitle files (default: the target-language subtitles of every library)")
    ap.add_argument("--run", action="store_true", help="write the repairs (default is a dry run)")
    ap.add_argument("--hook", action="store_true", help="repair the given subtitles as Bazarr's post-processing hook "
                                                        "(implies --run; see below)")
    ap.add_argument("--no-cache", action="store_true", help="check everything again, ignoring earlier verdicts")
    ap.add_argument("--json", action="store_true", help="check ONE subtitle and print the result as JSON")
    ap.add_argument("--out", help="with --json: write the corrected subtitle to this new .srt file")
    ap.add_argument("--log", help="also append every output line, with a timestamp, to this file; warnings then go "
                                  "there instead of to standard error")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="settings file (default: %(default)s)")
    return ap


def check_options(parser, args):
    if args.out and not args.json:
        parser.error("--out needs --json")
    if args.out and not args.out.lower().endswith(".srt"):
        parser.error("--out must name a .srt file")
    if args.json and (len(args.paths) != 1 or args.run or args.hook):
        parser.error("--json takes exactly one subtitle and does not combine with --run or --hook")
    if args.hook and not args.paths:
        parser.error("--hook needs the subtitles to repair")


def repair_command(argv):
    parser = repair_parser()
    args = parser.parse_args(argv)
    check_options(parser, args)
    plain_output(sys.stdout)
    try:
        prepare_log(args.log)
    except OSError as e:
        Report().line(f"error: cannot write the log {args.log}: {e}")
        return 1
    with logging_to(args.log, logging.WARNING):
        return repair(args, Report(args.log))


def repair(args, report):
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        report.line(f"error: {e}")
        return 1
    for warning in settings.warnings:
        log.warning("%s: %s", args.config, warning)
    repairer = Repairer(settings)
    try:
        if args.json:
            return json_result(args, settings, repairer)
        return Run(args, settings, repairer, report).execute()
    except (OSError, DataDirError) as e:          # the data folder cannot be used, for example
        report.line(f"error: {e}")
        return 1


def json_result(args, settings, repairer):
    targets, refused = explicit_targets(args.paths, settings.libraries)
    if refused:
        verdict, shown = refused[0].verdict, refused[0].target.shown
    else:
        target = targets[0]
        if args.out:
            verdict = repairer.copy(target.path, Path(args.out).absolute(), entries=target.entries)
        else:
            verdict = repairer.measure(target.path, entries=target.entries)
        shown = target.shown
    print(json.dumps(verdict.as_dict(shown)))
    return 1 if verdict.status is Status.ERROR else 0


class Run:
    """One repair command over the files it was given, or over the libraries. With --run it repairs under the data
    folder's lock, and repairs what hooks queued meanwhile before letting go."""

    def __init__(self, args, settings, repairer, report):
        self.args, self.settings, self.repairer, self.report = args, settings, repairer, report
        self.explicit = bool(args.paths)
        self.hook = args.hook
        self.backups = Backups(settings.tools.data_dir / BACKUP_DIR)
        self.results = []               # (verdict, cached) of every subtitle, for the summary and the exit status
        self.handled = set()            # queue entries this run has repaired or tried to
        self.fingerprint = fingerprint(settings)

    def execute(self):
        targets = []
        if self.explicit:
            targets, refused = explicit_targets(self.args.paths, self.settings.libraries)
            for result in refused:
                self.record(result)
        if self.args.run or self.hook:
            self.repair_locked(targets)
        else:
            self.check_all(targets, Checker(self.cache(), use_cached=not self.args.no_cache), self.measure)
        if not self.explicit:
            self.report.line(summary_line(self.results))
            if self.backups.folder and any(r.verdict.status is Status.FIXED and not r.cached for r in self.results):
                self.report.line(f"originals backed up in {self.backups.folder}")
        return 1 if any(r.verdict.status is Status.ERROR for r in self.results) else 0

    def repair_locked(self, targets):
        data_dir = datadir.prepare(self.settings.tools.data_dir)
        queue = HookQueue(data_dir / QUEUE_DIR)
        lock, busy = take_lock(data_dir / LOCK_FILE, HOOK_LOCK_WAIT if self.hook else LOCK_WAIT)
        if lock is None:
            self.cannot_lock(targets, queue, busy)
        else:
            with lock:
                checker = Checker(self.cache(), use_cached=not self.args.no_cache, store=True)
                self.check_all(targets, checker, self.repair)
                self.repair_queued(queue, checker)
                checker.save()
        self.repair_leftovers(queue, data_dir / LOCK_FILE)

    def cannot_lock(self, targets, queue, busy):
        if self.hook:
            for target in targets:
                queue.add(target.real)
                self.report.line(f"queued: {target.shown}  {QUEUED}")
        elif self.explicit:
            for target in targets:
                self.record(Result(target, Verdict(Status.ERROR, BUSY, str(busy), cacheable=False)))
        else:
            self.results.append(Result(None, Verdict(Status.ERROR, BUSY, str(busy), cacheable=False)))
            self.report.line(f"{Status.ERROR.value.upper():9} {BUSY} [{busy}]")

    def check_all(self, targets, checker, act):
        if self.explicit:
            for target in targets:
                self.record(checker.check(target, act))
            return
        for root in self.settings.libraries:
            if not root.is_dir():
                continue
            seen = set()
            for target in library_targets(self.settings, root):
                seen.add(lexical_key(target.real))
                self.record(checker.check(target, act))
            if checker.store:
                checker.prune(root, seen)

    def repair_queued(self, queue, checker):
        """Repair the files hooks queued, as the hook would; in hook mode their lines go to the log only."""
        while fresh := self.fresh_entries(queue):
            for entry, raw in fresh:
                self.handled.add(entry)
                targets, refused = explicit_targets([raw], self.settings.libraries)
                for result in refused + [checker.check(target, self.repair_as_hook) for target in targets]:
                    self.report.line(file_line(result, self.backups), on_output=not self.hook)
                queue.done(entry)

    def fresh_entries(self, queue):
        return [(entry, raw) for entry, raw in queue.entries() if entry not in self.handled]

    def repair_leftovers(self, queue, lock_path):
        """A hook may queue its file just as the lock's holder lets go: whoever finds the lock free repairs it."""
        while self.fresh_entries(queue):
            lock, _ = take_lock(lock_path, 0)
            if lock is None:
                return
            with lock:
                checker = Checker(self.cache(), store=True)
                self.repair_queued(queue, checker)
                checker.save()

    def record(self, result):
        self.results.append(Result(None, result.verdict, result.cached))     # not the folder listing it holds
        if self.explicit:
            self.report.line(file_line(result, self.backups))
        elif not (result.cached or result.verdict.quiet or result.verdict.status is Status.OK):
            self.report.line(scan_line(result))

    def cache(self):
        return VerdictCache(self.settings.tools.data_dir / CACHE_FILE, self.fingerprint).load()

    def measure(self, target):
        return self.repairer.measure(target.path, entries=target.entries, preview=True)

    def repair(self, target):
        return self.repairer.repair(target.path, self.backups, convert_smi=not self.hook, entries=target.entries)

    def repair_as_hook(self, target):
        return self.repairer.repair(target.path, self.backups, convert_smi=False, entries=target.entries)


def take_lock(path, wait):
    """(an ExitStack holding the lock at `path`, None), or (None, the TimeoutError) when another run kept it."""
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(process_lock(path, wait=wait))
    except TimeoutError as e:
        return None, e
    return stack, None


def plain_output(stream, windows=None):
    """Bazarr on Windows reads a hook's output in the console code page, so piped output there stays ASCII."""
    if not hasattr(stream, "reconfigure"):
        return
    if (WINDOWS if windows is None else windows) and not stream.isatty():
        stream.reconfigure(encoding="ascii", errors="backslashreplace")
    else:
        stream.reconfigure(errors="backslashreplace")


def prepare_log(log_file):
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def logging_to(log_file, level):
    """Log records at `level` and above go to `log_file`, or to standard error, while the block runs; Python's
    warnings go with them when there is a log file."""
    handler = logging.FileHandler(log_file, encoding="utf-8", delay=True) if log_file else logging.StreamHandler()
    handler.setFormatter(TimestampFormatter(LOG_FORMAT))
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(bool(log_file))
    try:
        yield
    finally:
        logging.captureWarnings(False)
        root.removeHandler(handler)
        root.setLevel(previous)
        handler.close()


class Stopped(Exception):
    pass


@contextlib.contextmanager
def stopped_by_sigterm():
    """SIGTERM (a service manager, docker stop) ends the server like Ctrl+C does."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum, frame):
        raise Stopped()

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def parse_port(text):
    if not (text.isascii() and text.isdigit()) or not 0 < int(text) < 65536:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number (1 to 65535)")
    return int(text)


def serve_parser():
    ap = argparse.ArgumentParser(
        prog="subsync-repair.py serve",
        description="The sync server: a local HTTP API on 127.0.0.1 (use that address, not localhost's ::1) for a "
                    "browser extension's \"Fix sync\" button. It accepts subtitles inside the configured libraries "
                    "only, and \"fix\" writes a new <subtitle>.synced.srt next to the subtitle. It has no password: "
                    "anything running on this machine can call it.")
    ap.add_argument("--port", type=parse_port, help="port to listen on (default: sync.port in the settings, or 8778)")
    ap.add_argument("--log", help="write the server's log to this file instead of standard error")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="settings file (default: %(default)s)")
    return ap


def serve_command(argv):
    args = serve_parser().parse_args(argv)
    try:
        prepare_log(args.log)
    except OSError as e:
        print(f"error: cannot write the log {args.log}: {e}", file=sys.stderr)
        return 1
    with logging_to(args.log, logging.INFO), stopped_by_sigterm():
        return serve(args)


def serve(args):
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        log.error("%s", e)
        return 1
    for warning in settings.warnings:
        log.warning("%s: %s", args.config, warning)
    try:
        datadir.prepare(settings.tools.data_dir)
    except (OSError, DataDirError) as e:
        log.error("cannot use the data folder: %s", e)
        return 1
    port = args.port or settings.port
    try:
        httpd = create_server(settings, port=port)
    except OSError as e:
        log.error("cannot listen on 127.0.0.1:%d: %s", port, e)
        return 1
    for problem in httpd.service.problems():
        log.warning("%s", problem)
    log.info("sync server on http://127.0.0.1:%d", httpd.server_address[1])
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, Stopped):
        pass
    finally:
        httpd.server_close()
    return 0
