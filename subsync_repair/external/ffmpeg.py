"""ffprobe and ffmpeg: what a video holds, its subtitle tracks, and turning any subtitle file into cues."""
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import NamedTuple

from subsync_repair.external import process
from subsync_repair.external.programs import ToolError
from subsync_repair.formats import ass, sami, srt
from subsync_repair.formats.decoding import decode_text
from subsync_repair.formats.languages import lang_of
from subsync_repair.formats.names import ASS_EXT, NON_DIALOGUE_FLAGS
from subsync_repair.storage.fileio import file_key
from subsync_repair.storage.memo import Memo

DEFAULT_FFMPEG = "ffmpeg"
DEFAULT_FFPROBE = "ffprobe"
FFPROBE_TIMEOUT = 60
FFMPEG_TIMEOUT = 600
MEMO_LIMIT = 500            # probes kept
TRACK_MEMO_LIMIT = 16       # extracted tracks kept: the next subtitle of the same video, or a fix after a measurement,
                            # reuses one, and their cues are large
TEXT_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"})
NON_DIALOGUE_TITLE = re.compile(rf"\b(?:{'|'.join(sorted(NON_DIALOGUE_FLAGS))})\b", re.I)
FRAME_COUNT_TAG = "NUMBER_OF_FRAMES"

log = logging.getLogger(__name__)


class ConversionError(Exception):
    pass


class Stream(NamedTuple):
    index: int
    codec: str
    lang: str
    title: str
    forced: bool
    frames: int | None      # cue count from the muxer's statistics tags, when it wrote them

    @property
    def dialogue(self):
        """A full dialogue track: not forced, and not titled as signs, songs or commentary."""
        return not self.forced and not NON_DIALOGUE_TITLE.search(self.title)

    @property
    def language(self):
        """The language code of the track's tag, or None for a missing or unknown one."""
        return lang_of(self.lang) if self.lang else None


class SubtitleText(NamedTuple):
    cues: list
    encoding: str           # what the file was decoded as
    dropped: tuple          # SAMI classes in other languages that were left out
    crlf: bool              # the file uses Windows line endings


def stream_of(raw):
    """The Stream of a subtitle track in ffprobe's output, or None for anything else or a malformed entry."""
    if not isinstance(raw, dict) or raw.get("codec_type") != "subtitle":
        return None
    try:
        index = int(raw["index"])
    except (KeyError, TypeError, ValueError):
        return None
    tags = raw.get("tags") or {}
    frames = next((v for k, v in tags.items() if k.upper().startswith(FRAME_COUNT_TAG)), None)
    return Stream(index=index, codec=raw.get("codec_name") or "", lang=tags.get("language") or "",
                  title=tags.get("title") or "", forced=bool((raw.get("disposition") or {}).get("forced")),
                  frames=int(frames) if str(frames).isdigit() else None)


class Media:
    """ffprobe and ffmpeg at the configured paths. Probes and extracted tracks are kept per version of a video."""

    def __init__(self, ffmpeg=DEFAULT_FFMPEG, ffprobe=DEFAULT_FFPROBE):
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self._probes = Memo(MEMO_LIMIT)
        self._tracks = Memo(TRACK_MEMO_LIMIT)

    def probe(self, video):
        """ffprobe's streams and duration of a video; {} when the file is missing or ffprobe cannot read it. Raises
        ToolError when ffprobe itself cannot run."""
        try:
            key = file_key(video)
        except OSError:
            return {}
        info = self._probes.get(key)
        if info is None:
            info = self._read_probe(video)
            if info:
                self._probes.put(key, info)
        return info

    def _read_probe(self, video):
        cmd = [self.ffprobe, "-v", "error", "-show_entries",
               "stream=index,codec_type,codec_name:stream_tags:stream_disposition=forced:format=duration",
               "-of", "json", video]
        r = self._call("ffprobe", self.ffprobe, cmd, FFPROBE_TIMEOUT, str(video))
        if r.returncode != 0:
            log.warning("ffprobe could not read %s: %s", video, process.tail(r))
            return {}
        try:
            info = json.loads(r.stdout or "{}")
        except ValueError:
            info = None
        if not isinstance(info, dict):
            log.warning("ffprobe wrote no JSON object for %s", video)
            return {}
        return info

    def duration(self, video):
        try:
            return float(self.probe(video)["format"]["duration"])
        except (KeyError, TypeError, ValueError):
            return None

    def subtitle_streams(self, video):
        streams = self.probe(video).get("streams")
        if not isinstance(streams, list):
            return []
        return [stream for stream in map(stream_of, streams) if stream is not None]

    def dialogue_tracks(self, video, accept):
        """The full dialogue text tracks of `video` whose language code passes `accept`."""
        return [s for s in self.subtitle_streams(video) if s.codec in TEXT_CODECS and s.dialogue and accept(s.language)]

    def tracks_by_size(self, video, accept, folder):
        """(stream, cues) of the dialogue tracks whose language passes `accept`, fullest first (ties in track order).
        When the muxer recorded every track's size, a track is only read when the caller asks for the next one."""
        tracks = self.dialogue_tracks(video, accept)
        if tracks and all(s.frames is not None for s in tracks):
            for stream in sorted(tracks, key=lambda s: -s.frames):
                cues = self.extract(video, [stream], folder).get(stream.index)
                if cues is not None:
                    yield stream, cues
            return
        extracted = self.extract(video, tracks, folder)
        yield from sorted(((s, extracted[s.index]) for s in tracks if s.index in extracted), key=lambda t: -len(t[1]))

    def extract(self, video, streams, folder):
        """{stream index: cues} of the given tracks, read in one pass over the video; a track ffmpeg cannot convert is
        left out, and a track read before from the same version of the video is not read again."""
        version = file_key(video)
        found, missing = {}, []
        for stream in streams:
            cues = self._tracks.get((version, stream.index))
            if cues is None:
                missing.append(stream)
            else:
                found[stream.index] = cues
        for index, cues in self._read_tracks(video, missing, folder).items():
            self._tracks.put((version, index), cues)
            found[index] = cues
        return found

    def _read_tracks(self, video, streams, folder):
        outs = {s.index: Path(folder) / f"track_{s.index}.srt" for s in streams}
        if not outs:
            return {}
        cmd = [self.ffmpeg, "-nostdin", "-y", "-v", "error", "-i", video]
        for index, out in outs.items():
            cmd += ["-map", f"0:{index}", out]
        if not self._run(cmd, list(outs.values())):
            if len(outs) == 1:
                return {}
            readable = {}
            for index, out in outs.items():
                if self.to_srt(video, out, index):
                    readable[index] = out
            outs = readable
        return {index: srt.read_srt(out) for index, out in outs.items()}

    def to_srt(self, source, out, stream_index=None):
        """Convert a subtitle file, or one track of a video, to an SRT file. False when ffmpeg fails."""
        cmd = [self.ffmpeg, "-nostdin", "-y", "-v", "error", "-i", source]
        if stream_index is not None:
            cmd += ["-map", f"0:{stream_index}"]
        return self._run(cmd + [out], [Path(out)])

    def read_subtitle(self, raw, ext, lang, folder):
        """The cues of a subtitle file's bytes `raw` in `lang`, whatever its format (`ext`) and encoding. For ASS/SSA
        only the spoken dialogue; for a SAMI file with several languages only `lang`. Raises UndecodableText,
        ValueError (a SAMI file without exactly one class in `lang`) or ConversionError."""
        decoded = decode_text(raw, lang, judge=ass.dialogue_text if ext in ASS_EXT else None)
        crlf = "\r\n" in decoded.text
        if ext == ".srt":
            return SubtitleText(srt.parse_srt(decoded.text), decoded.encoding, (), crlf)
        if ext in ASS_EXT:
            return SubtitleText(ass.dialogue_cues(decoded.text), decoded.encoding, (), crlf)
        kept, dropped = sami.keep_language(decoded.text, lang)
        return SubtitleText(self.convert(kept, ext, folder), decoded.encoding, dropped, crlf)

    def convert(self, text, ext, folder, name="converted"):
        """The cues of the subtitle `text` in a format that only ffmpeg reads (SAMI), or ConversionError."""
        source, out = Path(folder) / f"{name}{ext}", Path(folder) / f"{name}.srt"
        source.write_bytes(text.encode("utf-8"))
        if not self.to_srt(source, out):
            raise ConversionError(f"ffmpeg could not convert the {ext} file")
        return srt.read_srt(out)

    def _run(self, cmd, outputs):
        """True when ffmpeg wrote every one of `outputs`. Raises ToolError when ffmpeg cannot run."""
        r = self._call("ffmpeg", self.ffmpeg, cmd, FFMPEG_TIMEOUT, " ".join(map(str, cmd)))
        if r.returncode != 0:
            log.warning("ffmpeg failed: %s", process.tail(r))
            return False
        if not all(out.exists() for out in outputs):
            log.warning("ffmpeg wrote no output for %s", " ".join(map(str, cmd)))
            return False
        return True

    @staticmethod
    def _call(name, exe, cmd, timeout, timeout_detail):
        """The finished `cmd`. Raises ToolError when the program `name` (tools.`name`, which is `exe`) cannot be started
        or runs longer than `timeout` seconds."""
        try:
            return process.run(cmd, timeout)
        except subprocess.TimeoutExpired:
            raise ToolError(f"{name} took longer than {timeout} s", timeout_detail) from None
        except OSError as e:
            raise ToolError(f"{name} could not be started", f"tools.{name} is {exe!r}: {e}") from None
