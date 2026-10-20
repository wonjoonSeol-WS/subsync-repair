"""ffsubsync, which checks a reference subtitle against the audio of its video. It runs in
the interpreter that has ffsubsync, through ffsubsync_check.py."""
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

from subsync_repair.external import ffsubsync_check, process

FFSUBSYNC_TIMEOUT = 900
REFERENCE_SEARCH = 60            # seconds ffsubsync searches in either direction
REFERENCE_OFFSET_MAX = 1.0       # a reference that needs a larger offset is out of sync itself
REFERENCE_SCALE_TOLERANCE = 0.001
MIN_SCORE = 0.0                  # ffsubsync's own floor: at or below it the subtitle does not match the audio
CHECK_SCRIPT = Path(ffsubsync_check.__file__).resolve()
SCORE = re.compile(r"score:\s*(-?\d+(?:\.\d+)?)")
NOT_IMPORTABLE = "ffsubsync is not importable by tools.ffsubsync_python; add its folder to tools.ffsubsync_libs"


class ReferenceCheck(NamedTuple):
    ok: bool
    why: str
    lasting: bool       # the answer holds until the files change; False for a failure worth trying again
    detail: str = ""


class Ffsubsync:
    def __init__(self, python="", folders=(), timeout=FFSUBSYNC_TIMEOUT):
        self.python, self.folders, self.timeout = str(python or ""), tuple(folders), timeout

    def check(self, video, reference):
        """Whether `reference` is in sync with the audio of `video`: ffsubsync must find almost no offset."""
        if not self.python:
            return ReferenceCheck(ok=False, why="ffsubsync is not configured (tools.ffsubsync_python)", lasting=True)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            # -I: PYTHON* variables and the user's site folder must not decide what the check imports
            cmd = [self.python, "-I", CHECK_SCRIPT, json.dumps(list(self.folders)), video, reference,
                   Path(folder) / "checked.srt", REFERENCE_SEARCH]
            try:
                r = process.run(cmd, self.timeout)
            except subprocess.TimeoutExpired:
                return ReferenceCheck(ok=False, why=f"ffsubsync took longer than {self.timeout} s", lasting=False,
                                      detail=str(reference))
            except FileNotFoundError:
                return ReferenceCheck(ok=False, why="the ffsubsync interpreter was not found", lasting=False,
                                      detail=self.python)
            except OSError as e:
                return ReferenceCheck(ok=False, why="ffsubsync could not be started", lasting=False, detail=repr(e))
        return read_check_output(r.returncode, r.stdout or "", r.stderr or "")


def read_check_output(returncode, stdout, stderr):
    """The ReferenceCheck for what ffsubsync_check printed. It prints one RESULT line and ffsubsync one score; should a
    log carry more, the last of each counts."""
    result = last_result(stdout)
    if result is not None and result.get("importable") is False:
        return ReferenceCheck(ok=False, why=NOT_IMPORTABLE, lasting=False)
    scores = SCORE.findall(stdout + "\n" + stderr)
    failed = ReferenceCheck(ok=False, why="ffsubsync failed", lasting=False,
                            detail=(stderr or stdout).strip()[-process.OUTPUT_TAIL:])
    if returncode != 0 or result is None or not scores:
        return failed
    score = float(scores[-1])
    if score <= MIN_SCORE:
        return ReferenceCheck(ok=False, why=f"it does not match the audio (score {score:.1f})", lasting=True)
    offset, scale = result.get("offset_seconds"), result.get("framerate_scale_factor")
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (offset, scale))
    if not result.get("sync_was_successful") or not numbers:
        return failed
    good = abs(offset) <= REFERENCE_OFFSET_MAX and abs(scale - 1) <= REFERENCE_SCALE_TOLERANCE
    return ReferenceCheck(ok=good, why=f"reference offset {offset:+.2f}s scale {scale:.3f}", lasting=True)


def last_result(stdout):
    for line in reversed(stdout.splitlines()):
        if line.startswith(ffsubsync_check.RESULT_PREFIX):
            try:
                result = json.loads(line[len(ffsubsync_check.RESULT_PREFIX):])
            except ValueError:
                return None
            return result if isinstance(result, dict) else None
    return None
