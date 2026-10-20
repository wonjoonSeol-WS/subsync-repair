"""What a check of one subtitle concludes."""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

NOTE_MAX = 160


class Status(str, Enum):
    OK = "ok"                   # in sync
    FIXED = "fixed"
    WOULD_FIX = "would-fix"     # out of sync; a dry run or a measurement
    SKIP = "skip"               # left alone, for the reason in the note
    MISMATCH = "mismatch"       # the subtitle belongs to another episode or cut; left alone
    ERROR = "error"             # something failed; tried again next time


def clip(text, limit=NOTE_MAX):
    return text if len(text) <= limit else text[:limit - 3] + "..."


@dataclass(frozen=True)
class Verdict:
    status: Status
    note: str                   # fit to show anyone: no paths outside the subtitle's folder, no tool output
    detail: str = ""            # for the log: paths, tool output, exception text
    shifts: tuple = ()          # ((original time, shift), ...) of the correction alass found
    reference: str = ""         # what the subtitle was aligned to, such as "embedded en"
    output: Path | None = None  # the new file the correction was written to
    cacheable: bool = True      # the verdict holds until the subtitle, its video or its references change
    quiet: bool = False         # a routine outcome a library scan does not print

    def as_dict(self, path):
        return {"path": str(path), "status": self.status.value, "note": self.note,
                "shifts": [[round(t, 2), round(s, 2)] for t, s in self.shifts], "reference": self.reference or None,
                "output": str(self.output) if self.output else None}


class Refusal(Exception):
    """The subtitle is left alone; the check ends with `verdict`."""

    def __init__(self, verdict):
        super().__init__(verdict.note)
        self.verdict = verdict


def refuse(status, note, detail="", cacheable=None, quiet=False):
    """A Refusal; it is cacheable unless it is an error, or `cacheable` says otherwise."""
    cacheable = status is not Status.ERROR if cacheable is None else cacheable
    return Refusal(Verdict(status, note, detail, cacheable=cacheable, quiet=quiet))


def skip(note, **kwargs):
    return refuse(Status.SKIP, note, **kwargs)


def error(note, detail="", **kwargs):
    return refuse(Status.ERROR, note, detail, **kwargs)


def os_reason(e):
    """A short reason for an OSError, which may have no strerror."""
    return e.strerror or type(e).__name__


def file_error(e):
    """The verdict on a check that the OSError `e` stopped; it is tried again next time."""
    return Verdict(Status.ERROR, f"file error ({os_reason(e)})", repr(e), cacheable=False)
