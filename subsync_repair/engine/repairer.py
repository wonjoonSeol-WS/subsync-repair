"""Checking one subtitle against a reference in another language and, when its timing drifts, writing the correction
that the policy allows."""
import logging
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from subsync_repair.engine import policy
from subsync_repair.engine.references import Reference, ReferenceFinder
from subsync_repair.engine.verdicts import Refusal, Status, Verdict, clip, file_error, refuse, skip
from subsync_repair.engine.writers import Copy, InPlace, Measure
from subsync_repair.external.alass import Alass
from subsync_repair.external.audio_check import Ffsubsync
from subsync_repair.external.ffmpeg import ConversionError, Media, SubtitleText
from subsync_repair.external.programs import ToolError
from subsync_repair.formats import names, srt
from subsync_repair.formats.decoding import UndecodableText
from subsync_repair.storage.fileio import signature
from subsync_repair.storage.library import listing

NOT_REPAIRABLE = "not a subtitle this tool repairs (<video>.<language>[.<flag>].srt, .smi, .ass or .ssa)"
MISMATCH = "the subtitle does not match this video (different episode or cut?)"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alignment:
    sub: Path
    name: names.SubtitleName
    entries: tuple                  # the files of the subtitle's folder
    raw: bytes                      # the subtitle as it was checked
    text: SubtitleText              # its cues, encoding, line endings and left-out SAMI classes
    out: Path                       # alass's result
    reference: Reference
    correction: policy.Correction
    fixed: tuple                    # the corrected cues
    duration: float | None          # of the video, in seconds


def squash(text):
    """Text without whitespace differences; alass trims the leading space of continuation lines."""
    return "".join(text.split())


def repairable(path, entries):
    """The SubtitleName of a subtitle this tool repairs, or None: forced and sign tracks, the sync server's own copies
    and other files are not."""
    name, _ = names.identify(path, entries)
    return name if name and name.repairable else None


def verdict_key(sub, entries):
    """What a verdict on `sub` depends on: the subtitle, its video and the other subtitle files of that video, the
    sync server's copies aside."""
    name, video = names.identify(sub, entries)
    others = [p for n, p in names.sidecars(name.stem, entries) if p != sub and not n.synced] if name else []
    return "|".join(f"{p.name}={signature(p)}" for p in [sub] + ([video] if video else []) + others)


class Repairer:
    def __init__(self, settings, media=None, aligner=None, ffsubsync=None):
        t = settings.tools
        self.media = media or Media(t.ffmpeg, t.ffprobe)
        self.aligner = aligner or Alass(t.alass)
        self.references = ReferenceFinder(self.media, settings.embedded_order, settings.sibling_order,
                                          ffsubsync or Ffsubsync(t.ffsubsync_python, t.ffsubsync_libs))

    def measure(self, sub, entries=None, preview=False):
        """The verdict on `sub`; nothing is written. A `preview` foresees repair(): an .ass that its .srt repairs is not
        measured on its own."""
        return self.check(sub, Measure(preview), entries)

    def copy(self, sub, dest, replaceable="", allowed=None, entries=None):
        """Write the corrected subtitle to the new file `dest`; the original is left alone."""
        return self.check(sub, Copy(dest, replaceable, allowed), entries)

    def repair(self, sub, backups, convert_smi=True, entries=None):
        """Repair `sub` in place, after backing up every file that changes."""
        return self.check(sub, InPlace(backups, self.media, convert_smi), entries)

    def check(self, sub, writer, entries=None):
        """The verdict on `sub`, with `entries` the files of its folder when the caller has them."""
        sub = Path(sub)
        try:
            return self._check(sub, writer, entries)
        except Refusal as refusal:
            return refusal.verdict
        except ToolError as e:
            return Verdict(Status.ERROR, e.message, e.detail, cacheable=False)
        except OSError as e:
            return file_error(e)
        except Exception as e:      # a bug must not end a library scan or the sync server's worker
            log.exception("checking %s failed", sub)
            return Verdict(Status.ERROR, "unexpected error", repr(e), cacheable=False)

    def _check(self, sub, writer, entries):
        entries = tuple(listing(sub.parent) if entries is None else entries)
        name, video = names.identify(sub, entries)
        if not name or not name.repairable:
            raise skip(NOT_REPAIRABLE, cacheable=False)
        writer.before(sub, name, entries)
        seen = signature(sub)
        raw = sub.read_bytes()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            alignment = self.align(sub, name, video, entries, raw, Path(folder))
            try:
                verdict = self.conclude(alignment, writer, seen)
            except Refusal as refusal:          # a refused correction is reported as well
                verdict = refusal.verdict
        return replace(verdict, shifts=alignment.correction.pieces, reference=alignment.reference.label)

    def align(self, sub, name, video, entries, raw, folder):
        """Run alass for `sub` against its reference. Raises Refusal when there is nothing trustworthy to compare."""
        try:
            text = self.media.read_subtitle(raw, name.ext, name.lang, folder)
        except UndecodableText as e:
            raise skip(f"cannot decode the {name.ext} file ({e})") from None
        except (ValueError, ConversionError) as e:
            raise skip(str(e)) from None
        work = folder / "working.srt"
        work.write_bytes(srt.format_with_text(text.cues).encode("utf-8"))
        original = srt.read_srt(work)
        if len(original) < policy.MIN_CUES:
            raise skip("the file has too few cues to judge")
        reference = self.references.find(name, video, entries, folder)
        out = folder / "aligned.srt"
        self.aligner.align(reference.path, work, out)
        fixed = srt.read_srt(out)
        if [squash(c.text) for c in original] != [squash(c.text) for c in fixed]:
            raise skip("alass changed the subtitle text")
        correction = policy.Correction([c.start for c in original], [c.start for c in fixed])
        duration = self.media.duration(video) if video else None
        return Alignment(sub, name, entries, raw, text, out, reference, correction, tuple(fixed), duration)

    @staticmethod
    def conclude(alignment, writer, seen):
        """The verdict policy.py reaches on the alignment: in sync, refused, or the writer's."""
        correction = alignment.correction
        if policy.in_sync(correction):
            return Verdict(Status.OK, "in sync")
        shifts = policy.describe(correction.pieces)
        if policy.mismatch(correction):
            raise refuse(Status.MISMATCH, f"{MISMATCH} ({clip(shifts)})")
        problem = policy.problem(correction, srt.last_end(alignment.fixed), alignment.duration)
        if problem:
            raise skip(f"{problem} ({clip(shifts)})")
        return writer.write(alignment, f"{shifts} [ref: {alignment.reference.label}]", seen)
