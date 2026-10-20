"""What a repair run prints: one line per subtitle on standard output, and the same lines with a timestamp in the --log
file, which also gets the log records in the same timestamp format."""
import collections
import datetime
import logging
import sys
import time
from pathlib import Path

from subsync_repair.engine.verdicts import Status

SUMMARY_ORDER = ("fixed", "would-fix", "ok", "skip", "mismatch", "error", "cached")

log = logging.getLogger(__name__)


def timestamp(seconds=None):
    """Local time with its UTC offset, to the second."""
    moment = time.time() if seconds is None else seconds
    return datetime.datetime.fromtimestamp(moment).astimezone().isoformat(timespec="seconds")


class TimestampFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return timestamp(record.created)


class Report:
    def __init__(self, log_file=None, out=None):
        self.log_file = Path(log_file) if log_file else None
        self.out = out or sys.stdout

    def line(self, text, on_output=True):
        """`text` on standard output (with `on_output`) and in the log."""
        if on_output:
            print(text, file=self.out, flush=True)
        if self.log_file:
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(f"{timestamp()} {text}\n")
            except OSError as e:
                log.warning("cannot write the log %s: %s", self.log_file, e)


def scan_line(result):
    """A line of a library scan: status, the path inside the library and the note."""
    verdict = result.verdict
    return f"{verdict.status.value.upper():9} {result.target.shown}  {verdict.note}{extras(result)}"


def file_line(result, backups=None):
    """The one line for a file named on the command line, status first so that a wrapper can act on it."""
    return f"{result.verdict.status.value}: {result.target.shown}  {result.verdict.note}{extras(result, backups)}"


def extras(result, backups=None):
    verdict, text = result.verdict, ""
    if result.cached:
        text += " (cached)"
    if backups and backups.folder and verdict.status is Status.FIXED and not result.cached:
        text += f" [originals in {backups.folder}]"
    if verdict.status is Status.ERROR and verdict.detail:
        text += f" [{' / '.join(verdict.detail.splitlines())}]"
    return text


def summary_line(results):
    """'summary: fixed 1, ok 3, cached 20'"""
    tally = collections.Counter("cached" if r.cached else r.verdict.status.value for r in results)
    counts = [f"{key} {tally[key]}" for key in SUMMARY_ORDER if tally[key]]
    return "summary: " + (", ".join(counts) if counts else "no subtitles to check")
