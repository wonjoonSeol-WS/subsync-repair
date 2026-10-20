"""ASS/SSA subtitles: reading their dialogue, and shifting their events by the piecewise amounts the aligner found."""
import bisect
import re
from typing import NamedTuple

from subsync_repair.formats import srt
from subsync_repair.formats.decoding import decode_text

ASS_TIME = re.compile(r"(\d+):(\d\d):(\d\d)\.(\d{1,3})")
DEFAULT_FORMAT = ("layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text")
MOVED_EPSILON = 0.005       # shifts smaller than this do not count as moved
SAME_TIMING = 0.1           # seconds within which an event and an SRT cue start together
SAME_TIMING_SHARE = 0.8     # share of the SRT cues that must start with an event for both files to share timing
# Signs, karaoke and drawings have no counterpart in a reference, so they do not take part in the alignment.
NOT_DIALOGUE_TAGS = re.compile(r"\\(?:pos|move|org|i?clip)\(|\\p[1-9]|\\(?:k|kf|ko|K)\d")
NOT_DIALOGUE_STYLES = re.compile(r"(?<![a-z])(?:signs?|songs?|kara(?:oke)?|op|ed|opening|ending|titles?|insert|lyrics?)"
                                 r"(?![a-z])", re.I)


class ShiftResult(NamedTuple):
    text: str
    events: int         # Dialogue lines in the file
    timed: int          # of those, the ones whose times could be read
    moved: int          # of those, the ones that moved by more than MOVED_EPSILON
    before_zero: int    # of those, the ones the shift would move before 0:00 (left as they were)


def shift_for(t, pieces):
    """The shift of the last piece that starts at or before `t`; the first piece also covers everything before it.
    An event takes its piece's shift, which may differ from its own cue's by up to policy.PIECE_TOLERANCE."""
    if not pieces:
        return 0.0
    i = bisect.bisect_right([start for start, _ in pieces], t)
    return pieces[max(i - 1, 0)][1]


def ass_seconds(value):
    m = ASS_TIME.fullmatch(value.strip())
    if not m:
        return None
    return srt.seconds(*m.groups())


def centiseconds(t):
    return int(round(t * 100))


def ass_time(t):
    cs = centiseconds(t)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def event_format(lines):
    """The field names of the [Events] Format line, lower case; the standard ASS order when there is none."""
    in_events = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_events = stripped.lower() == "[events]"
        elif in_events and stripped.lower().startswith("format:"):
            return tuple(f.strip().lower() for f in stripped.split(":", 1)[1].split(","))
    return DEFAULT_FORMAT


def event_fields(line, fmt):
    """The fields of a Dialogue line (the last one, the text, may contain commas), or None when it is malformed."""
    fields = line.split(":", 1)[1].lstrip().split(",", len(fmt) - 1)
    return fields if len(fields) == len(fmt) else None


def dialogue_lines(text):
    """[(line number, fields)] of the Dialogue events, and the field names."""
    lines = text.split("\n")
    fmt = event_format(lines)
    events = [(i, event_fields(line, fmt)) for i, line in enumerate(lines) if line.startswith("Dialogue:")]
    return lines, events, fmt


def dialogue_text(text):
    """Just the spoken text of an ASS/SSA file: its header and style tables are Latin whatever the language."""
    _, events, fmt = dialogue_lines(text)
    return "\n".join(fields[fmt.index("text")] for _, fields in events if fields and "text" in fmt)


def event_times(fields, fmt):
    if not fields or "start" not in fmt or "end" not in fmt:
        return None, None
    return ass_seconds(fields[fmt.index("start")]), ass_seconds(fields[fmt.index("end")])


def dialogue_cues(text):
    """The spoken dialogue as cues sorted by start, without signs, karaoke and drawings."""
    _, events, fmt = dialogue_lines(text)
    cues = []
    for _, fields in events:
        start, end = event_times(fields, fmt)
        if start is None or end is None:
            continue
        body = fields[fmt.index("text")].rstrip("\r") if "text" in fmt else ""
        style = fields[fmt.index("style")] if "style" in fmt else ""
        if NOT_DIALOGUE_TAGS.search(body) or NOT_DIALOGUE_STYLES.search(style):
            continue
        spoken = srt.spoken(body.replace("\\h", " "))
        if spoken:
            cues.append(srt.Cue(start, end, spoken))
    return sorted(cues, key=lambda c: c.start)


def event_starts(text):
    """Start times of every Dialogue event whose times can be read."""
    _, events, fmt = dialogue_lines(text)
    return sorted(start for start, _ in (event_times(fields, fmt) for _, fields in events) if start is not None)


def shift_text(text, pieces):
    """Every Dialogue event moved by the shift of its piece; comments and everything else stay as they are."""
    lines, events, fmt = dialogue_lines(text)
    timed = moved = before_zero = 0
    for i, fields in events:
        start, end = event_times(fields, fmt)
        if start is None or end is None:
            continue
        timed += 1
        shift = shift_for(start, pieces)
        if centiseconds(start + shift) < 0 or centiseconds(end + shift) < 0:
            before_zero += 1
            continue
        moved += abs(shift) > MOVED_EPSILON
        fields[fmt.index("start")], fields[fmt.index("end")] = ass_time(start + shift), ass_time(end + shift)
        lines[i] = "Dialogue: " + ",".join(fields)
    return ShiftResult("\n".join(lines), len(events), timed, moved, before_zero)


def shift_problem(result):
    """Why a shift must not be written, or None: every Dialogue event has to be understood and stay after 0:00, and
    some have to move."""
    if result.timed < result.events:
        return f"{result.events - result.timed} of its {result.events} events have times this tool cannot read"
    if result.before_zero:
        return f"{result.before_zero} of its {result.events} events would start before 0:00"
    if not result.moved:
        return "none of its events would move"
    return None


def shares_timing(text, starts):
    """Most of the SRT cue `starts` begin together with a Dialogue event of the ASS `text`."""
    events = event_starts(text)
    if not starts or not events:
        return False
    together = 0
    for t in starts:
        i = bisect.bisect_left(events, t)
        together += any(abs(events[j] - t) <= SAME_TIMING for j in (i - 1, i) if 0 <= j < len(events))
    return together >= len(starts) * SAME_TIMING_SHARE


def read(raw, lang):
    """The Decoded text of an ASS/SSA file in `lang`, judged by its dialogue; raises UndecodableText."""
    return decode_text(raw, lang, judge=dialogue_text)
