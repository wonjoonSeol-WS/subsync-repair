"""The reference a subtitle is aligned to, always in another language than the subtitle: a dialogue track inside its
video, or else a subtitle file next to it, which is checked against the audio first because it can drift itself."""
from dataclasses import dataclass, replace
from pathlib import Path

from subsync_repair.engine.verdicts import Refusal, Status, refuse, skip
from subsync_repair.formats import names, srt
from subsync_repair.formats.decoding import UndecodableText, decode_text
from subsync_repair.formats.languages import ANY_LANGUAGE
from subsync_repair.storage.fileio import file_key
from subsync_repair.storage.memo import Memo

MIN_REFERENCE_CUES = 50
OTHER_CUT_SLACK = 60.0      # a reference running this many seconds past its video is from another cut
CHECK_MEMO_LIMIT = 500
EMBEDDED, SIBLING = "embedded", "sibling"
NO_REFERENCE = ("no reference (no usable track in the video and no subtitle file next to it in a reference "
                "language)")


@dataclass(frozen=True)
class Reference:
    path: Path      # the reference as clean UTF-8 SRT
    kind: str       # EMBEDDED or SIBLING
    lang: str

    @property
    def label(self):
        return f"{self.kind} {self.lang}"


class ReferenceFinder:
    """`embedded_order` and `sibling_order` list reference languages best first; ANY_LANGUAGE stands for every other
    language present."""

    def __init__(self, media, embedded_order, sibling_order, ffsubsync):
        self.media, self.ffsubsync = media, ffsubsync
        self.embedded_order, self.sibling_order = tuple(embedded_order), tuple(sibling_order)
        self._checks = Memo(CHECK_MEMO_LIMIT)

    def find(self, name, video, entries, folder):
        """A trusted reference for the subtitle `name`, or Refusal. An embedded track is trusted as it is, without an
        audio check; a sibling file is used only once ffsubsync finds it in sync."""
        found = self.embedded(video, name.lang, folder) if video else None
        if found:
            return found
        refusals = []
        for lang, path in self.siblings(name, entries):
            try:
                return self.verified_sibling(video, lang, path, folder)
            except Refusal as refusal:
                refusals.append(refusal)
        if not refusals:
            raise skip(NO_REFERENCE, quiet=True)
        lasting = all(refusal.verdict.cacheable for refusal in refusals)
        raise Refusal(replace(refusals[0].verdict, cacheable=lasting))

    def embedded(self, video, own_lang, folder):
        """The fullest dialogue track in the first embedded reference language that has one with enough cues."""
        for accept in acceptors(self.embedded_order, own_lang):
            for stream, cues in self.media.tracks_by_size(video, accept, folder):
                if len(cues) >= MIN_REFERENCE_CUES:
                    out = Path(folder) / "reference.srt"
                    out.write_bytes(srt.format_with_text(cues).encode("utf-8"))
                    return Reference(out, EMBEDDED, stream.language)
                break
        return None

    def siblings(self, name, entries):
        """(language, path) of every full .srt for the same video in a reference language, best first."""
        candidates = [(n, p) for n, p in names.sidecars(name.stem, entries)
                      if n.ext == ".srt" and n.repairable]
        for accept in acceptors(self.sibling_order, name.lang):
            for found, path in names.preferred([(n, p) for n, p in candidates if accept(n.lang)]):
                yield found.lang, path

    def verified_sibling(self, video, lang, path, folder):
        """The sibling subtitle `path` as a Reference, once it passes the checks against the video; else Refusal."""
        if not video:
            raise skip(f"no video to check the sibling {lang} reference against")
        reference = Reference(clean_reference(path, lang, folder), SIBLING, lang)
        last, duration = srt.last_end(srt.read_srt(reference.path)), self.media.duration(video)
        if duration and last is not None and last > duration + OTHER_CUT_SLACK:
            raise skip(f"the reference runs {last / 60:.0f} min but the video is {duration / 60:.0f} min")
        check = self.checked(video, path, reference.path)
        if not check.ok:
            raise refuse(Status.SKIP, f"reference not verified: {check.why}", check.detail, cacheable=check.lasting)
        return reference

    def checked(self, video, source, reference):
        """ffsubsync's check of `reference` (made from the file `source`) against `video`; a lasting answer is kept
        while both files stay the same."""
        key = (file_key(video), file_key(source))
        check = self._checks.get(key)
        if check is None:
            check = self.ffsubsync.check(video, reference)
            if check.lasting:
                self._checks.put(key, check)
        return check


def acceptors(order, own_lang):
    """A test of a language code for each entry of a reference language `order`; the subtitle's own language and,
    for ANY_LANGUAGE, the languages listed by name never pass."""
    named = {lang for lang in order if lang != ANY_LANGUAGE}
    for lang in order:
        if lang == ANY_LANGUAGE:
            yield lambda code: code is not None and code != own_lang and code not in named
        elif lang != own_lang:
            yield lambda code, lang=lang: code == lang


def clean_reference(path, lang, folder):
    """A subtitle file next to the video as clean UTF-8 SRT (it may be CP932 or UTF-16, or have blank lines in cues)."""
    try:
        text = decode_text(Path(path).read_bytes(), lang).text
    except UndecodableText:
        raise skip("the reference subtitle cannot be read") from None
    out = Path(folder) / "reference.srt"
    out.write_bytes(srt.normalize_srt(text).encode("utf-8"))
    return out
