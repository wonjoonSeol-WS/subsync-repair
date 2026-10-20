"""File-name rules shared by the tools: <video name>.<language>[.<flag>...].<ext>, where the language is the first tag
after the video's name and later tags are flags (hi, sdh, forced, ...). A tag may join flags with "-" or "_"
(en-forced, x-forced)."""
import os
from dataclasses import dataclass

from subsync_repair.formats.languages import REGION, lang_of

# Jellyfin's video extensions without disc images, split archives, playlists and .strm pointers: ffprobe reads none.
VIDEO_EXT = frozenset({
    ".3g2", ".3gp", ".amv", ".asf", ".avi", ".bivx", ".divx", ".dv", ".dvr-ms", ".f4v", ".fli", ".flv", ".m2t",
    ".m2ts", ".m2v", ".m4v", ".mk3d", ".mkv", ".mov", ".mp4", ".mpe", ".mpeg", ".mpg", ".mts", ".mxf", ".nsv", ".nuv",
    ".ogm", ".ogv", ".pva", ".qt", ".rec", ".rm", ".rmvb", ".svq3", ".tp", ".ts", ".ty", ".viv", ".vob", ".vp3",
    ".webm", ".wmv", ".wtv", ".xvid"})
SUBTITLE_EXT = frozenset({".srt", ".smi", ".ass", ".ssa"})
ASS_EXT = frozenset({".ass", ".ssa"})
FLAG_TAGS = frozenset({"hi", "sdh", "cc", "forced", "default", "full", "sign", "signs", "song", "songs", "commentary"})
NON_DIALOGUE_FLAGS = frozenset({"forced", "sign", "signs", "song", "songs", "commentary"})
SYNCED_TAG = "synced"


@dataclass(frozen=True)
class SubtitleName:
    stem: str             # the video's name
    lang: str
    flags: tuple = ()     # lower-case tags after the language, split at "-" and "_"
    ext: str = ".srt"     # lower-case extension

    @property
    def dialogue(self):
        """A full dialogue track: not forced, signs, songs or commentary."""
        return not NON_DIALOGUE_FLAGS.intersection(self.flags)

    @property
    def synced(self):
        """The corrected copy the sync server writes."""
        return self.ext == ".srt" and self.flags[-1:] == (SYNCED_TAG,)

    @property
    def repairable(self):
        """A subtitle this tool repairs, or uses as a reference: a full dialogue track that is no corrected copy."""
        return self.dialogue and not self.synced


def split_ext(name):
    base, ext = os.path.splitext(name)
    return base, ext.lower()


def is_flag(token):
    parts = REGION.split(token.lower())
    return any(part in FLAG_TAGS for part in parts) or (token.isalpha() and lang_of(token) is None)


def parse(name, stem=None):
    """The SubtitleName of a file name, or None. Without the video's `stem` it is guessed: the language is the leftmost
    language tag followed only by flags."""
    base, ext = split_ext(name)
    if ext not in SUBTITLE_EXT:
        return None
    if stem is not None:
        if base[:len(stem)].casefold() != stem.casefold() or base[len(stem):len(stem) + 1] != ".":
            return None
        return tagged(stem, base[len(stem) + 1:].split("."), ext)
    tokens = base.split(".")
    for i in range(1, len(tokens)):
        if lang_of(tokens[i]) and all(is_flag(t) for t in tokens[i + 1:]):
            return tagged(".".join(tokens[:i]), tokens[i:], ext)
    return None


def tagged(stem, tags, ext):
    """The SubtitleName of the tags after the video's name: the first names the language, the rest are flags."""
    lang = lang_of(tags[0])
    if not lang:
        return None
    joined = [part for part in REGION.split(tags[0].lower())[1:] if part in FLAG_TAGS]
    rest = [part for tag in tags[1:] for part in REGION.split(tag.lower()) if part]
    return SubtitleName(stem, lang, tuple(joined + rest), ext)


def videos(entries):
    return [p for p in entries if p.suffix.lower() in VIDEO_EXT]


def video_of(sub, entries):
    """The video in `entries` that the subtitle file `sub` belongs to: the longest video name that its name extends."""
    owners = [v for v in videos(entries) if parse(sub.name, v.stem)]
    return max(owners, key=lambda v: len(v.stem), default=None)


def identify(sub, entries):
    """(SubtitleName or None, video or None) for the subtitle file `sub` among the files of its folder."""
    video = video_of(sub, entries)
    return parse(sub.name, video.stem if video else None), video


def sidecars(stem, entries):
    """[(SubtitleName, path)] of the subtitle files in `entries` that belong to a video named `stem`, sorted by name."""
    found = ((parse(p.name, stem), p) for p in sorted(entries))
    return [(name, p) for name, p in found if name]


def preferred(found):
    """[(SubtitleName, path)] best first: a file without flags, then the fuller (larger) file, then by name."""
    return sorted(found, key=lambda item: (bool(item[0].flags), -size_of(item[1]), item[1].name))


def twin(sub, name, entries, extensions):
    """The preferred other full subtitle for the same video and language with one of `extensions`, or None."""
    found = [(n, p) for n, p in sidecars(name.stem, entries)
             if p != sub and n.lang == name.lang and n.ext in extensions and n.repairable]
    return next((p for _, p in preferred(found)), None)


def paired(sub, name, entries, extensions, own):
    """The twin of `sub` with one of `extensions` whose own twin with one of `own` is `sub` again: the two belong
    together, as an .srt and the .ass made from the same timing do. Else None."""
    other = twin(sub, name, entries, extensions)
    if other is None or twin(other, parse(other.name, name.stem), entries, own) != sub:
        return None
    return other


def renamed(name, lang, ext=".srt"):
    """The file name for the same video and flags in another language."""
    return ".".join([name.stem, lang, *name.flags]) + ext


def synced_name(sub):
    """The name of the corrected copy of `sub`: its own name with ".synced.srt", so every subtitle gets its own copy."""
    base, ext = split_ext(sub.name)
    return f"{base}{'' if ext == '.srt' else ext}.{SYNCED_TAG}.srt"


def size_of(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return 0
