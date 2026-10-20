"""What a check does with a correction: report it, write it to a new file, or write it in place after a backup (an
.srt with its .ass twin, an .ass, or a .smi as new .srt files)."""
import contextlib
import datetime
import itertools
import os
from pathlib import Path
from typing import NamedTuple

from subsync_repair.engine.verdicts import Status, Verdict, error, os_reason, skip
from subsync_repair.external.ffmpeg import ConversionError
from subsync_repair.formats import ass, names, sami, srt
from subsync_repair.formats.decoding import Decoded, UndecodableText, decode_text, utf8_like
from subsync_repair.storage.fileio import atomic_write_bytes, is_link, remove_file, signature, sweep_stale

BACKUP_PREFIX = "repair-"
WRITTEN, SAME, TAKEN = "written", "same", "taken"
SMI_NOTE = ".smi -> .srt"
HOOK_SMI = "the hook leaves a .smi in place; a scan or a manual run converts it to .srt"
LEFT_LIBRARY = "the subtitle's folder left the library while it was checked"


def smi_notes(alignment):
    if alignment.name.ext != ".smi":
        return []
    dropped = alignment.text.dropped
    return [SMI_NOTE] + ([f"other languages in the .smi: {', '.join(dropped)}"] if dropped else [])


def with_notes(note, notes):
    return note + (f" ({', '.join(notes)})" if notes else "")


class AssPartner(NamedTuple):
    path: Path
    raw: bytes
    decoded: Decoded


def ass_partner(srt_path, srt_name, entries, starts):
    """(the .ass repaired together with the .srt at `srt_path`, whose cues start at `starts`, or None; why its .ass is
    left alone, or None). The two must pair both ways (names.paired), and the .ass must be no link, decode and share
    the .srt's timing. Both sides ask this: the .srt when it is repaired, the .ass when it steps aside for its .srt."""
    path = names.paired(srt_path, srt_name, entries, names.ASS_EXT, {".srt"})
    if path is None:
        return None, None
    if is_link(path):
        return None, "it is a link"
    raw = path.read_bytes()
    try:
        decoded = ass.read(raw, srt_name.lang)
    except UndecodableText:
        return None, "it does not decode"
    if not ass.shares_timing(decoded.text, starts):
        return None, "its timing does not match the .srt"
    return AssPartner(path, raw, decoded), None


def leave_ass_to_its_srt(sub, name, entries):
    """Refuse, quietly, to repair an .ass on its own when its .srt repairs it (see ass_partner)."""
    if name.ext not in names.ASS_EXT:
        return
    srt_path = names.paired(sub, name, entries, {".srt"}, names.ASS_EXT)
    if srt_path is None:
        return
    try:
        cues = srt.parse_srt(decode_text(srt_path.read_bytes(), name.lang).text)
    except UndecodableText:
        return
    partner, _ = ass_partner(srt_path, names.parse(srt_path.name, name.stem), entries,
                             [c.start for c in cues if c.text.strip()])
    if partner is not None and partner.path == sub:
        raise skip(f"fixed together with its .srt twin {srt_path.name}", quiet=True)


def shifted_ass(decoded, raw, pieces):
    """(the .ass that was `raw`, read as `decoded`, with every event moved by its piece's shift, as bytes, None), or
    (None, why it must not be written)."""
    result = ass.shift_text(decoded.text, pieces)
    problem = ass.shift_problem(result)
    return (None, problem) if problem else (utf8_like(result.text, raw, decoded.encoding), None)


class Backups:
    """Originals kept in this run's own folder, <root>/repair-<time>, by their full path (drive included). The folder
    is made on first use; a backup is never replaced."""

    def __init__(self, root, now=datetime.datetime.now):
        self.root, self.now = Path(root), now
        self.folder = None

    def path_for(self, path):
        path = Path(os.path.abspath(path))
        drive = path.drive.replace(":", "").replace("\\", "_").replace("/", "_").strip("_")
        return self.run_folder() / drive / path.relative_to(path.anchor)

    def run_folder(self):
        if self.folder is None:
            self.root.mkdir(parents=True, exist_ok=True)
            stamp = self.now().strftime("%Y%m%d-%H%M%S")
            for n in itertools.count(1):
                folder = self.root / f"{BACKUP_PREFIX}{stamp}{'' if n == 1 else f'-{n}'}"
                try:
                    folder.mkdir()
                except FileExistsError:
                    continue
                self.folder = folder
                break
        return self.folder

    def keep(self, path, data):
        """Back up `data`, the bytes of `path` as they were checked, and return where. When this run already backed up
        other bytes of the same file, these go next to them as <name>.~2~, <name>.~3~, ..."""
        first = self.path_for(path)
        first.parent.mkdir(parents=True, exist_ok=True)
        for n in itertools.count(1):
            dest = first if n == 1 else first.with_name(f"{first.name}.~{n}~")
            outcome = store(dest, data)
            if outcome == WRITTEN:
                with contextlib.suppress(OSError):
                    st = os.stat(path)
                    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
            if outcome != TAKEN:
                return dest


def store(dest, data):
    """WRITTEN when `data` went into the free name `dest`, SAME when `dest` already holds it, TAKEN when it holds
    other bytes."""
    while True:
        try:
            return SAME if dest.read_bytes() == data else TAKEN
        except FileNotFoundError:
            pass
        try:
            atomic_write_bytes(dest, data, exclusive=True)
            return WRITTEN
        except FileExistsError:
            continue


class Measure:
    """Writes nothing. As a `preview` of InPlace it, too, leaves an .ass to the .srt that repairs it."""

    def __init__(self, preview=False):
        self.preview = preview

    def before(self, sub, name, entries):
        if self.preview:
            leave_ass_to_its_srt(sub, name, entries)

    def write(self, alignment, note, seen):
        return Verdict(Status.WOULD_FIX, with_notes(note, smi_notes(alignment)))


class Copy:
    """Writes the corrected subtitle to a new file and leaves the original alone. An existing file is only replaced
    when its signature is `replaceable` (a copy this program wrote before). `allowed(dest)`, when given, must still
    hold right before and right after the write."""

    def __init__(self, dest, replaceable="", allowed=None):
        self.dest, self.replaceable, self.allowed = Path(dest), replaceable, allowed

    def may_replace(self):
        try:
            return bool(self.replaceable) and not is_link(self.dest) and signature(self.dest) == self.replaceable
        except OSError:
            return False

    def before(self, sub, name, entries):
        sweep_stale(self.dest.parent)
        if os.path.lexists(self.dest) and not self.may_replace():
            raise skip(f"{self.dest.name} already exists", cacheable=False)

    def write(self, alignment, note, seen):
        self.require_allowed()
        data = alignment.out.read_bytes()
        replace = self.may_replace()
        try:
            atomic_write_bytes(self.dest, data, exclusive=not replace, follow_links=False)
        except FileExistsError:
            raise skip(f"{self.dest.name} appeared while the subtitle was being checked", cacheable=False) from None
        if self.allowed and not self.allowed(self.dest):
            with contextlib.suppress(OSError):
                os.unlink(self.dest)
            raise error(LEFT_LIBRARY, str(self.dest))
        return Verdict(Status.FIXED, f"{note} (written to {self.dest.name}, original untouched)", output=self.dest)

    def require_allowed(self):
        if self.allowed and not self.allowed(self.dest):
            raise error(LEFT_LIBRARY, str(self.dest))


class InPlace:
    """Repairs the subtitle itself after backing up every file it changes; an .srt and the .ass it pairs with are
    repaired together. With `convert_smi=False` (the hook) a .smi is left alone instead of being replaced by .srt
    files. Files other than the subtitle are never written through a link."""

    def __init__(self, backups, media, convert_smi=True):
        self.backups, self.media, self.convert_smi = backups, media, convert_smi

    def before(self, sub, name, entries):
        leave_ass_to_its_srt(sub, name, entries)
        sweep_stale(sub.parent)
        if name.ext != ".smi":
            return
        if not self.convert_smi:
            raise skip(HOOK_SMI, cacheable=False)
        target = sub.with_suffix(".srt")
        existing = names.twin(sub, name, entries, {".srt"}) or (target if os.path.lexists(target) else None)
        if existing:
            raise skip(f"{existing.name} already exists next to the .smi", cacheable=False)

    def write(self, alignment, note, seen):
        if signature(alignment.sub) != seen:
            raise skip("changed while it was being checked", cacheable=False)
        if alignment.name.ext == ".srt":
            notes, output = self.fix_srt(alignment), None
        elif alignment.name.ext == ".smi":
            notes, output = self.fix_smi(alignment)
        else:
            notes, output = self.fix_ass(alignment), None
        return Verdict(Status.FIXED, with_notes(note, notes), output=output)

    def fix_srt(self, alignment):
        corrected = srt_bytes(alignment)
        twin, notes = self.twin_shift(alignment)
        backup = self.backups.keep(alignment.sub, alignment.raw)
        if twin is None:
            atomic_write_bytes(alignment.sub, corrected)
            return notes
        path, original, shifted = twin
        twin_backup = self.backups.keep(path, original)
        atomic_write_bytes(path, shifted, follow_links=False)
        try:
            atomic_write_bytes(alignment.sub, corrected)
        except OSError as e:
            try:
                atomic_write_bytes(path, original, follow_links=False)          # both files or neither
            except OSError as again:
                raise error(f"the .srt could not be written and {path.name} could not be put back: restore it from "
                            f"the backup", f"backups: {twin_backup}, {backup}; {e!r}; {again!r}",
                            cacheable=True) from None
            raise
        return ["+ .ass"]

    @staticmethod
    def twin_shift(alignment):
        """((path, original bytes, shifted bytes) or None, notes) for the .ass repaired together with the .srt (see
        ass_partner), which takes the same shift."""
        partner, why = ass_partner(alignment.sub, alignment.name, alignment.entries, alignment.correction.original)
        if partner is None:
            return None, [f"the .ass was left alone: {why}"] if why else []
        shifted, problem = shifted_ass(partner.decoded, partner.raw, alignment.correction.pieces)
        if problem:
            return None, [f"the .ass was left alone: {problem}"]
        return (partner.path, partner.raw, shifted), []

    def fix_ass(self, alignment):
        try:
            decoded = ass.read(alignment.raw, alignment.name.lang)
        except UndecodableText:
            raise skip("the .ass file does not decode") from None
        shifted, problem = shifted_ass(decoded, alignment.raw, alignment.correction.pieces)
        if problem:
            raise skip(f"the .ass was left alone: {problem}")
        self.backups.keep(alignment.sub, alignment.raw)
        atomic_write_bytes(alignment.sub, shifted)
        return ["dialogue events shifted"]

    def fix_smi(self, alignment):
        """The .smi becomes <name>.srt, and each other language it holds its own <video>.<language>.srt with its
        timing unchanged. When one of those cannot be written, the .smi stays as well."""
        target = alignment.sub.with_suffix(".srt")
        extras, kept_because = self.other_languages(alignment)
        backup = self.backups.keep(alignment.sub, alignment.raw)
        created, current = [], target
        try:
            for current, data in [(target, srt_bytes(alignment))] + extras:
                atomic_write_bytes(current, data, exclusive=True, follow_links=False, like=alignment.sub)
                created.append(current)
            if kept_because is None:
                current = alignment.sub
                alignment.sub.unlink()
        except FileExistsError:
            self.take_back(created, backup)
            raise skip(f"{current.name} appeared next to the .smi", cacheable=False) from None
        except OSError as e:
            self.take_back(created, backup)
            action = "remove" if current == alignment.sub else "write"
            raise error(f"could not {action} {current.name} ({os_reason(e)}); nothing was changed", repr(e)) from None
        notes = [SMI_NOTE] + [f"also {path.name}" for path, _ in extras]
        return notes + ([f"the .smi was kept: {kept_because}"] if kept_because else []), target

    def other_languages(self, alignment):
        """([(path, bytes)] of the .srt files for the other languages of a .smi, or [] and why they cannot be made."""
        if not alignment.text.dropped:
            return [], None
        text = decode_text(alignment.raw, alignment.name.lang).text
        extras = []
        for cls in alignment.text.dropped:
            lang = sami.class_language(text, cls)
            if lang is None:
                return [], f"{cls} is in no known language"
            path = alignment.sub.with_name(names.renamed(alignment.name, lang))
            if os.path.lexists(path) or path in (p for p, _ in extras):
                return [], f"{path.name} already exists"
            try:
                only_lang = sami.keep_language(text, lang)[0]
                cues = self.media.convert(only_lang, ".smi", alignment.out.parent, f"class-{lang}")
            except (ValueError, ConversionError):
                return [], f"{cls} could not be converted"
            extras.append((path, srt_file(srt.format_with_text(cues), alignment)))
        return extras, None

    @staticmethod
    def take_back(created, backup):
        """Remove the files a failed .smi conversion made; when that fails too, the error says where the original is."""
        for path in created:
            try:
                remove_file(path)
            except OSError as e:
                raise error(f"could not take back {path.name}: check it against the backup of the .smi",
                            f"backup: {backup}; {e!r}", cacheable=True) from None


def srt_bytes(alignment):
    """The corrected SRT as the file to write."""
    return srt_file(alignment.out.read_bytes().decode("utf-8-sig"), alignment)


def srt_file(text, alignment):
    """SRT `text` with the line endings of the checked subtitle, as UTF-8 (see decoding.utf8_like)."""
    text = text.replace("\r\n", "\n")
    if alignment.text.crlf:
        text = text.replace("\n", "\r\n")
    return utf8_like(text, alignment.raw, alignment.text.encoding)
