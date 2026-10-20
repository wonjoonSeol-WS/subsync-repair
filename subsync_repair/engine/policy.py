"""When an alignment is out of sync, and when its correction is believable enough to write."""
THRESHOLD = 0.6          # a cue moved by more than this many seconds means the subtitle was out of sync
PIECE_TOLERANCE = 0.35   # a cue whose shift differs from its piece's first by more than this starts a new piece
MAX_SHIFT = 180.0        # no cue may move further
MAX_SEGMENTS = 6         # with more pieces than this ...
SLIDE_OK = 5.0           # ... no cue may move more than this
# A mismatch (a subtitle of another episode or cut): some cue moves more than MISMATCH_SHIFT and no piece holds
# KEPT_SHARE of the cues.
MISMATCH_SHIFT = 30.0
KEPT_SHARE = 0.5
EARLY_WINDOW = 30.0      # pieces that start within this many seconds of the first cue ...
EARLY_STEPS = 3          # ... this many or more are an unstable start,
GENTLE_STEP = 1.0        # unless they differ by at most this much, all in one direction (a frame-rate drift)
END_SLACK = 30.0         # the corrected subtitle may not run more than this far past the end of the video
STACK_LIMIT = 2          # the fix may not add this many cues sharing a start time with an earlier cue
ZERO_LIMIT = 3           # ... nor push more than this many cues to time zero
SAME_MOMENT = 0.001      # start times closer than this are the same moment
MIN_CUES = 20            # fewer cues are too few to judge
FRAME_RATE = "the shift grows steadily through the file (frame-rate difference?)"


class Correction:
    """What alass did to a subtitle: each cue's shift (corrected start minus original start, to the millisecond of SRT
    times), and the pieces those shifts form: runs of consecutive cues whose shift stays within PIECE_TOLERANCE of the
    run's first."""

    def __init__(self, original_starts, fixed_starts):
        self.original = tuple(original_starts)
        self.fixed = tuple(fixed_starts)
        self.shifts = tuple(round(after - before, 3) for before, after in zip(self.original, self.fixed, strict=True))
        self.runs = runs_of(self.shifts)

    @property
    def pieces(self):
        """((start of the piece's first cue, its shift), ...)"""
        return tuple((self.original[first], self.shifts[first]) for first, _ in self.runs)

    @property
    def worst(self):
        return max((abs(s) for s in self.shifts), default=0.0)

    @property
    def longest_run(self):
        return max((count for _, count in self.runs), default=0)


def runs_of(shifts):
    """[(index of the first cue, number of cues)] of the pieces of `shifts`."""
    runs = []
    for i, shift in enumerate(shifts):
        if not runs or abs(shift - shifts[runs[-1][0]]) > PIECE_TOLERANCE:
            runs.append([i, 0])
        runs[-1][1] += 1
    return [tuple(run) for run in runs]


def in_sync(correction):
    return correction.worst <= THRESHOLD


def gentle(shifts):
    """Consecutive shifts that differ by at most GENTLE_STEP each, all in one direction: a frame-rate drift."""
    steps = [b - a for a, b in zip(shifts, shifts[1:])]
    return all(abs(d) <= GENTLE_STEP for d in steps) and (all(d > 0 for d in steps) or all(d < 0 for d in steps))


def steady_ramp(pieces):
    return len(pieces) > 1 and gentle([shift for _, shift in pieces])


def mismatch(correction):
    """The correction is too large and keeps no long piece of the subtitle together: it belongs to another video."""
    return (correction.worst > MISMATCH_SHIFT and correction.longest_run < len(correction.shifts) * KEPT_SHARE
            and not steady_ramp(correction.pieces))


def unstable_start(pieces):
    """The correction changes again and again right after the first cue, as when alass squeezes the first cues."""
    if not pieces:
        return False
    first = pieces[0][0]
    early = [shift for start, shift in pieces if start < first + EARLY_WINDOW]
    return len(early) >= EARLY_STEPS and not gentle(early)


def correction_problem(correction):
    """Why the shape of the correction is not believable, or None."""
    pieces = correction.pieces
    if correction.worst > MAX_SHIFT or (len(pieces) > MAX_SEGMENTS and correction.worst > SLIDE_OK):
        return FRAME_RATE if steady_ramp(pieces) else "implausible correction"
    if unstable_start(pieces):
        return f"the correction keeps changing in the first {EARLY_WINDOW:.0f} s"
    return None


def shared_starts(starts):
    """How many cues start at the same moment as an earlier cue."""
    return len(starts) - len({round(s / SAME_MOMENT) for s in starts})


def disorder(starts):
    return sum(1 for a, b in zip(starts, starts[1:]) if b < a - SAME_MOMENT)


def at_zero(starts):
    return sum(1 for s in starts if s <= SAME_MOMENT)


def result_problem(correction, fixed_end, duration):
    """Why the corrected subtitle no longer looks like a subtitle for its video, or None. What the original already had
    (cues out of order, several at time zero, stacked cues) is not blamed on the fix."""
    original, fixed = correction.original, correction.fixed
    if disorder(fixed) > disorder(original):
        return "cue order changed"
    if at_zero(fixed) - at_zero(original) > ZERO_LIMIT:
        return "many cues pushed to time zero"
    stacked = shared_starts(fixed) - shared_starts(original)
    if stacked >= STACK_LIMIT:
        return f"{stacked} cues stacked onto shared start times"
    if duration and fixed_end is not None and fixed_end > duration + END_SLACK:
        return f"corrected subtitle would run past the end of the {duration / 60:.0f} min video"
    return None


def problem(correction, fixed_end, duration):
    """Why a correction that is not a mismatch must not be written, or None."""
    return correction_problem(correction) or result_problem(correction, fixed_end, duration)


def describe(pieces):
    """'shifts 0:05 +2.0s, 1:40 -7.0s'"""
    return "shifts " + ", ".join(f"{clock(t)} {shift:+.1f}s" for t, shift in pieces)


def clock(t):
    total = round(t)
    return f"{total // 60}:{total % 60:02d}"
