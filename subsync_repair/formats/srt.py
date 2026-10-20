"""SubRip (.srt) reading and writing."""
import re
from dataclasses import dataclass
from pathlib import Path

TIMING = re.compile(r"\s*(-?)(\d+):(\d+):(\d+)(?:[,.](\d+))?\s*-->\s*(-?)(\d+):(\d+):(\d+)(?:[,.](\d+))?")
BLANK_LINE = re.compile(r"\n[ \t]*\n")
MARKUP = re.compile(r"\{[^}]*\}|<[^>]+>")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def seconds(h, m, s, fraction):
    return int(h) * 3600 + int(m) * 60 + int(s) + (int(fraction) / 10 ** len(fraction) if fraction else 0.0)


def parse_srt(text):
    """Every cue that has a timing line; a timing line always starts a new cue. A block without one continues the
    previous cue (ffmpeg writes blank lines inside some cues). A cue with a negative time is left out."""
    cues, current = [], None
    for block in BLANK_LINE.split(text.replace("\r\n", "\n").replace("\r", "\n").strip()):
        lines = [line.rstrip() for line in block.split("\n") if line.strip()]
        timings = [i for i, line in enumerate(lines) if TIMING.match(line)]
        if not timings:
            if current is not None:
                current[2].extend(lines)
            continue
        for n, i in enumerate(timings):
            end = timings[n + 1] if n + 1 < len(timings) else len(lines)
            body = lines[i + 1:end]
            if end < len(lines) and body and body[-1].strip().isdigit():    # the number of the next cue
                body.pop()
            current = timed(TIMING.match(lines[i]), body)
            if current is not None:
                cues.append(current)
    return [Cue(start, end, "\n".join(body)) for start, end, body in cues]


def timed(match, body):
    """[start, end, body lines] of a timing line, or None when one of its times is negative."""
    if match.group(1) or match.group(6):
        return None
    return [seconds(*match.group(2, 3, 4, 5)), seconds(*match.group(7, 8, 9, 10)), body]


def read_srt(path):
    return parse_srt(Path(path).read_text(encoding="utf-8-sig", errors="replace"))


def format_time(t):
    ms = round(max(0.0, t) * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def format_srt(cues):
    return "".join(f"{n}\n{format_time(c.start)} --> {format_time(c.end)}\n{c.text}\n\n" for n, c in enumerate(cues, 1))


def format_with_text(cues):
    """Clean SRT of the cues that have text: renumbered, one blank line between cues."""
    return format_srt(c for c in cues if c.text.strip())


def normalize_srt(text):
    """The subtitle text as clean SRT for the aligner."""
    return format_with_text(parse_srt(text))


def spoken(text):
    """A cue's text without markup: SRT/HTML tags and ASS override blocks removed, \\N as a line break."""
    return MARKUP.sub("", text).replace("\\N", "\n").replace("\\n", "\n").strip()


def last_end(cues):
    return max((c.end for c in cues), default=None)
