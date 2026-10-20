"""Fixtures shared by the tests: subtitle text, fake tools, and settings built in a temporary folder. The real
config.json is never read."""
import logging
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path

from subsync_repair import config
from subsync_repair.engine import policy, references
from subsync_repair.engine.repairer import Repairer
from subsync_repair.engine.writers import Backups
from subsync_repair.external import ffmpeg
from subsync_repair.external.audio_check import ReferenceCheck
from subsync_repair.external.programs import ToolError
from subsync_repair.formats import ass, srt

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
CUES = policy.MIN_CUES + 10
STARTS = [10.0 * i for i in range(CUES)]
REFERENCE_CUES = references.MIN_REFERENCE_CUES + 10
KOREAN = [f"안녕하세요 잘 지내세요 {i}" for i in range(CUES)]
JAPANESE = [f"こんにちは、元気ですか {i}" for i in range(CUES)]
GERMAN = [f"Guten Tag, wie geht es dir {i}" for i in range(CUES)]
ENGLISH = [f"Hello there, how are you {i}" for i in range(REFERENCE_CUES)]
UNDECODABLE = bytes(range(128, 256)) * 4
CHROME_ID = "abcdefghijklmnopabcdefghijklmnop"
FIREFOX_ID = "0f8e3b6a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
# alass against a video's own track, for a subtitle of another episode: large block offsets, and inside each block a
# shift that drifts on because alass also applied a frame-rate scale of about 1.04.
ANOTHER_EPISODE_BLOCKS = (-35.0, -113.0, -104.0, -74.0, -91.0, -82.0)
FRAME_RATE_DRIFT = 0.04
ASS_HEADER = ("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
              "Style: Default,Arial,20\n\n[Events]\n"
              "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def completed(stdout="", stderr="", code=0):
    return types.SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def starts_with_gap(count, after, gap):
    """Cues every 5 s, with `gap` more seconds after cue number `after` (where the original lost or gained time)."""
    starts, t = [], 0.0
    for i in range(count):
        starts.append(t)
        t += 5.0 + (gap if i == after else 0.0)
    return starts


def ass_events(starts, texts, style="Default", kind="Dialogue", extra=""):
    """ASS event lines for cues 1.5 s long."""
    return "".join(f"{kind}: 0,{ass.ass_time(s)},{ass.ass_time(s + 1.5)},{style},,0,0,0,,{extra}{t}\n"
                   for s, t in zip(starts, texts))


def srt_of(starts, texts):
    return srt.format_srt(srt.Cue(s, s + 1.5, t) for s, t in zip(starts, texts))


def cues_of(starts, texts):
    return srt.parse_srt(srt_of(starts, texts))


def track(count=REFERENCE_CUES, texts=ENGLISH):
    """Cues of an embedded track: STARTS, then one cue a second after them."""
    starts = (STARTS + [STARTS[-1] + 100.0 + i for i in range(count)])[:count]
    return cues_of(starts, (texts * (count // len(texts) + 1))[:count])


def another_episode_shift(i, start, starts):
    """The shift alass gives cue `i` (at `start`) of a subtitle of another episode with `len(starts)` cues."""
    block = i * len(ANOTHER_EPISODE_BLOCKS) // len(starts)
    first = starts[block * len(starts) // len(ANOTHER_EPISODE_BLOCKS)]
    return ANOTHER_EPISODE_BLOCKS[block] + FRAME_RATE_DRIFT * (start - first)


def make_settings(root, data_dir, target, **cfg):
    return config.parse_settings({"target_language": target, "libraries": [str(root)],
                                  "tools": {"data_dir": str(data_dir)}, **cfg}, platform="linux", env={})


class FakeMedia(ffmpeg.Media):
    """No ffprobe: embedded tracks and the duration come from the test. Like Media, tracks_by_size yields the tracks
    whose language passes, fullest first. ffmpeg conversion (.smi) is the real one."""

    def __init__(self, tracks=None, duration=None):
        super().__init__()
        self.tracks = tracks or {}          # {language: [cue list per track]}
        self.video_duration = duration      # seconds, or None
        self.asked = []                     # (video name, language) of every language with tracks that was asked for

    def duration(self, video):
        return self.video_duration

    def tracks_by_size(self, video, accept, folder):
        found = []
        for index, (lang, cues) in enumerate((lang, cues) for lang, tracks in self.tracks.items() for cues in tracks):
            if accept(lang):
                self.asked.append((Path(video).name, lang))
                found.append((ffmpeg.Stream(index, "subrip", lang, "", False, None), cues))
        yield from sorted(found, key=lambda item: -len(item[1]))


class FakeAligner:
    """A fake alass: moves each cue by `shift` (seconds, or a function of the start), or to `starts_fn(starts)`, and
    may change the texts."""

    def __init__(self, shift=5.0, texts=None, starts_fn=None, fail=None):
        self.shift, self.texts, self.starts_fn, self.fail = shift, texts, starts_fn, fail
        self.calls = []                     # (reference cues, working cues)

    def align(self, reference, work, out):
        cues = srt.read_srt(work)
        self.calls.append((srt.read_srt(reference), cues))
        if self.fail:
            raise ToolError(*self.fail)
        starts = [c.start for c in cues]
        moved = self.starts_fn(starts) if self.starts_fn else [
            s + (self.shift(s) if callable(self.shift) else self.shift) for s in starts]
        out.write_text(srt_of(moved, self.texts or [c.text for c in cues]), encoding="utf-8")


class FakeFfsubsync:
    """A fake ffsubsync check: `result`, or `decide(reference text)` when it is set."""

    def __init__(self, ok=True, why="reference offset +0.10s scale 1.000", lasting=True):
        self.result = ReferenceCheck(ok=ok, why=why, lasting=lasting)
        self.decide = None
        self.calls = []

    def check(self, video, reference):
        self.calls.append((Path(video).name, Path(reference).name))
        return self.decide(Path(reference).read_text(encoding="utf-8")) if self.decide else self.result


def starts_of(path):
    return [c.start for c in srt.read_srt(path)]


def hard_links_work(folder):
    """Whether `folder` supports hard links, which exclusive writes use first."""
    probe = Path(folder) / "link-probe"
    probe.write_bytes(b"")
    try:
        os.link(probe, Path(folder) / "link-probe-2")
    except OSError:
        return False
    finally:
        for name in ("link-probe", "link-probe-2"):
            (Path(folder) / name).unlink(missing_ok=True)
    return True


def make_link(link, target):
    """A folder link: a junction on Windows (no privilege needed), a symlink elsewhere. Skips the test if impossible."""
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            Path(link).symlink_to(target, target_is_directory=True)
    except (OSError, ImportError, AttributeError) as e:
        raise unittest.SkipTest(f"cannot create a link here: {e}") from None


def make_file_link(link, target):
    """A symlink to a file, which may be missing. Skips the test where symlinks need a privilege (Windows)."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        raise unittest.SkipTest(f"cannot create a file symlink here: {e}") from None


class TempFolder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class LibraryCase(TempFolder):
    """A library with one show folder: a video, its subtitle in the target language (Korean unless a test class says
    otherwise) and an English track inside the video that alass will find 5 s apart."""

    TARGET = "ko"
    SUBTITLE = "Show S01E01.ko.srt"
    TEXTS = KOREAN

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "library"
        self.dir = self.root / "Show"
        self.dir.mkdir(parents=True)
        self.data = self.tmp / "data"
        self.backups = Backups(self.tmp / "backup")
        self.settings = make_settings(self.root, self.data, self.TARGET)
        self.video = self.write("Show S01E01.mkv", b"")
        self.sub = self.write(self.SUBTITLE, srt_of(STARTS, self.TEXTS))
        self.media = FakeMedia({"en": [track()]})
        self.aligner = FakeAligner()
        self.ffsubsync = FakeFfsubsync()
        self.repairer = self.repairer_for(self.settings)

    def repairer_for(self, settings):
        return Repairer(settings, self.media, self.aligner, self.ffsubsync)

    def write(self, name, content, folder=None):
        path = (folder or self.dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def starts_of(self, path):
        return starts_of(path)

    def backed_up(self):
        root = self.backups.root
        return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


class NoForeignLogging(unittest.TestCase):
    """Fails a test that leaves a handler on the root logger or changes its level."""

    def setUp(self):
        super().setUp()
        root = logging.getLogger()
        before = (list(root.handlers), root.level)
        self.addCleanup(lambda: self.assertEqual((list(root.handlers), root.level), before, "logging was changed"))
