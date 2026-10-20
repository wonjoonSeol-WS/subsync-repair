"""Tests for the repair command (subsync_repair/cli.py, run as subsync-repair.py): library scans, the verdict cache,
single files as Bazarr's post-processing hook passes them, the hook queue, JSON output and usage errors. alass is a
fake; one end-to-end test uses the real ffmpeg and ffprobe."""
import dataclasses
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from subsync_repair import __version__, cli
from subsync_repair.config import ROOT
from subsync_repair.engine import batch, cache, policy, references, writers
from subsync_repair.engine.repairer import Repairer
from subsync_repair.engine.verdicts import Status, Verdict, refuse
from subsync_repair.external import audio_check, programs
from subsync_repair.external.ffmpeg import Media
from subsync_repair.formats import ass
from subsync_repair.storage import fileio
from subsync_repair.storage.hook_queue import HookQueue
from subsync_repair.storage.library import lexical_key
from support import (ASS_HEADER, ENGLISH, HAVE_FFMPEG, JAPANESE, KOREAN, STARTS, FakeFfsubsync, LibraryCase,
                     NoForeignLogging, another_episode_shift, ass_events, make_link, srt_of, track)

STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d "
TIMESTAMPED = re.compile(STAMP)


class Command(LibraryCase, NoForeignLogging):
    def setUp(self):
        super().setUp()
        self.config = self.tmp / "config.json"
        self.write_config()
        self.log = self.tmp / "logs" / "hook" / "subsync-repair.log"

    def write_config(self, **extra):
        cfg = {"target_language": self.TARGET, "libraries": [str(self.root)], "skip_dirs": ["Extras"],
               "tools": {"data_dir": str(self.data)}, **extra}
        self.config.write_text(json.dumps(cfg), encoding="utf-8")

    def main(self, *argv):
        out = io.StringIO()
        with mock.patch.object(cli, "Repairer", self.repairer_for), mock.patch("sys.stdout", out):
            code = cli.main([*argv, "--config", str(self.config)])
        return code, out.getvalue().splitlines()

    def hook(self, *paths):
        return self.main("--hook", "--log", str(self.log), *map(str, paths))

    def cached(self):
        path = self.data / cli.CACHE_FILE
        return json.loads(path.read_text(encoding="utf-8"))["files"] if path.exists() else {}

    @staticmethod
    def cache_key(path):
        return lexical_key(os.path.realpath(path))

    def logged(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []



def same_file(a, b):
    return os.path.realpath(a) == os.path.realpath(b)


def shown(path):
    return str(Path(path))


class LibraryScan(Command):
    def test_the_scan_finds_target_language_subtitles_and_skips_listed_folders_and_the_servers_copies(self):
        self.write("Show S01E01.ko.synced.srt", srt_of(STARTS, KOREAN))
        self.write("Show S01E01.ko.forced.srt", srt_of(STARTS, KOREAN))
        self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        self.write("Bonus.ko.srt", srt_of(STARTS, KOREAN), folder=self.dir / "Extras")
        settings = cli.load_settings(self.config)
        self.assertEqual([t.path for t in batch.library_targets(settings, self.root)], [self.sub])

    def test_a_link_inside_the_library_is_not_followed(self):
        outside = self.tmp / "outside"
        self.write("Elsewhere.mkv", b"", folder=outside)
        elsewhere = self.write("Elsewhere.ko.srt", srt_of(STARTS, KOREAN), folder=outside)
        make_link(self.root / "Linked", outside)
        before = elsewhere.read_bytes()
        code, lines = self.main("--run")
        self.assertEqual(code, 0)
        self.assertEqual(elsewhere.read_bytes(), before)
        self.assertEqual(len(self.aligner.calls), 1)
        self.assertFalse(any("Elsewhere" in line for line in lines))

    @unittest.skipIf(os.name == "nt", "creating symlinks needs a privilege on Windows")
    def test_a_subtitle_that_is_a_link_to_a_file_outside_the_library_is_not_scanned(self):
        outside = self.write("Show S01E02.ko.srt", srt_of(STARTS, KOREAN), folder=self.tmp / "outside")
        (self.dir / "Show S01E02.ko.srt").symlink_to(outside)
        self.write("Show S01E02.mkv", b"")
        before = outside.read_bytes()
        self.main("--run")
        self.assertEqual(len(self.aligner.calls), 1)
        self.assertEqual(outside.read_bytes(), before)

    def test_the_default_is_a_dry_run_with_a_summary(self):
        code, lines = self.main()
        self.assertEqual(code, 0)
        self.assertEqual(self.starts_of(self.sub), STARTS)
        self.assertTrue(lines[0].startswith(f"WOULD-FIX {shown('Show/Show S01E01.ko.srt')}  shifts 0:00 +5.0s"))
        self.assertEqual(lines[-1], "summary: would-fix 1")
        self.assertEqual(self.cached(), {})

    def test_run_repairs_and_reports_the_backup_folder(self):
        code, lines = self.main("--run")
        self.assertEqual(code, 0)
        self.assertEqual(self.starts_of(self.sub), [s + 5.0 for s in STARTS])
        self.assertEqual(lines[-2], "summary: fixed 1")
        self.assertRegex(lines[-1], r"^originals backed up in .*repair-\d{8}-\d{6}$")

    def test_a_fixed_file_is_remembered_and_not_checked_again(self):
        self.main("--run")
        self.aligner.calls.clear()
        _, lines = self.main("--run")
        self.assertEqual(self.aligner.calls, [])
        self.assertEqual(lines, ["summary: cached 1"])

    def test_a_changed_subtitle_is_checked_again(self):
        self.main("--run")
        self.sub.write_text(srt_of(STARTS, [f"새 글 {t}" for t in KOREAN]), encoding="utf-8")
        self.aligner.calls.clear()
        self.main("--run")
        self.assertEqual(len(self.aligner.calls), 1)

    def test_a_changed_video_alone_is_checked_again(self):
        self.aligner.shift = 0.0
        self.main("--run")
        self.video.write_bytes(b"a replaced video file")
        self.aligner.calls.clear()
        self.main("--run")
        self.assertEqual(len(self.aligner.calls), 1)

    def test_a_changed_reference_file_alone_is_checked_again(self):
        self.media.tracks = {}
        reference = self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        self.aligner.shift = 0.0
        self.main("--run")
        self.assertEqual(self.cached()[self.cache_key(self.sub)]["status"], "ok")
        reference.write_text(srt_of([s + 0.5 for s in STARTS], JAPANESE), encoding="utf-8")
        self.aligner.calls.clear()
        self.main("--run")
        self.assertEqual(len(self.aligner.calls), 1)

    def test_changed_settings_make_every_cached_verdict_stale(self):
        self.aligner.shift = 0.0
        self.main("--run")
        self.write_config(reference_languages=["en"])
        self.aligner.calls.clear()
        self.main("--run")
        self.assertEqual(len(self.aligner.calls), 1)

    def test_a_dry_run_neither_writes_nor_caches(self):
        self.main()
        self.assertFalse((self.data / cli.CACHE_FILE).exists())

    def test_no_cache_checks_everything_again_and_stores_what_it_found(self):
        other = self.write("Show S01E02.ko.srt", srt_of(STARTS, KOREAN))
        self.main("--run")
        self.sub.write_text(srt_of(STARTS, KOREAN), encoding="utf-8")
        self.main("--run")
        before = self.cached()[self.cache_key(self.sub)]
        self.aligner.calls.clear()
        self.aligner.shift = 7.0
        self.main("--run", "--no-cache", str(self.sub))
        self.assertEqual(len(self.aligner.calls), 1)
        self.assertEqual(set(self.cached()), {self.cache_key(self.sub), self.cache_key(other)})
        self.assertNotEqual(self.cached()[self.cache_key(self.sub)], before)
        self.assertIn("+7.0s", self.cached()[self.cache_key(self.sub)]["note"])

    def test_an_error_is_retried_next_time_and_the_exit_code_says_so(self):
        self.aligner.fail = ("alass failed", "stderr")
        code, _ = self.main("--run")
        self.assertEqual(code, 1)
        self.assertEqual(self.cached(), {})
        self.aligner.fail = None
        self.assertEqual(self.main("--run")[1][0][:5], "FIXED")

    def test_a_transient_refusal_is_not_cached(self):
        transient = refuse(Status.SKIP, "changed meanwhile", cacheable=False)
        with mock.patch.object(Repairer, "align", side_effect=transient):
            self.main("--run")
        self.assertEqual(self.cached(), {})

    def test_skips_are_cached_until_the_files_change(self):
        self.aligner.shift = lambda s: 0.0 if s < 100 else 400.0
        self.main("--run")
        self.assertEqual(self.cached()[self.cache_key(self.sub)]["status"], "skip")
        self.assertEqual(self.main("--run")[1], ["summary: cached 1"])

    def test_quiet_and_in_sync_results_are_not_printed_by_a_scan(self):
        self.media.tracks = {}
        self.assertEqual(self.main()[1], ["summary: skip 1"])
        self.media.tracks = {"en": [track()]}
        self.aligner.shift = 0.0
        self.assertEqual(self.main()[1], ["summary: ok 1"])

    def test_the_cache_is_saved_after_every_so_many_new_verdicts(self):
        self.main("--run")
        for i in range(2, 7):
            self.write(f"Show S01E0{i}.ko.srt", srt_of(STARTS, KOREAN))
        sizes = []
        real_save = cache.VerdictCache.save

        def save(verdicts):
            sizes.append(len(verdicts.files))
            return real_save(verdicts)

        with mock.patch.object(batch, "SAVE_EVERY", 2), mock.patch.object(cache.VerdictCache, "save", save):
            self.main("--run")
        self.assertEqual(sizes, [3, 5, 6])

    def test_a_cache_that_cannot_be_saved_is_a_warning_and_the_scan_goes_on(self):
        with mock.patch.object(fileio, "atomic_write_bytes", side_effect=OSError(28, "disk full")), \
                self.assertLogs("subsync_repair.engine.cache", "WARNING") as logs:
            code, lines = self.main("--run")
        self.assertEqual((code, lines[-2]), (0, "summary: fixed 1"))
        self.assertIn("disk full", logs.output[0])

    def test_a_converted_smi_is_remembered_by_its_new_srt(self):
        self.sub.unlink()
        smi = self.write("Show S01E01.ko.smi", b"")
        new = self.dir / "Show S01E01.ko.srt"

        def convert(repairer, sub, backups, convert_smi=True, entries=None):
            smi.unlink()
            new.write_text(srt_of(STARTS, KOREAN), encoding="utf-8")
            return Verdict(Status.FIXED, ".smi -> .srt", output=new)

        with mock.patch.object(Repairer, "repair", convert):
            self.main("--run", str(smi))
        self.assertEqual(set(self.cached()), {self.cache_key(new)})
        self.aligner.calls.clear()
        self.assertEqual(self.main("--run")[1], ["summary: cached 1"])

    def test_entries_of_files_that_are_gone_are_pruned_and_other_languages_are_kept(self):
        other = self.write("Show S01E02.ko.srt", srt_of(STARTS, KOREAN))
        japanese = self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        self.main("--run")
        self.hook(japanese)
        other.unlink()
        self.main("--run")
        self.assertEqual(set(self.cached()), {self.cache_key(self.sub), self.cache_key(japanese)})

    def test_an_unreadable_or_foreign_cache_is_ignored_and_only_a_broken_one_is_reported(self):
        path = self.data / cli.CACHE_FILE
        path.parent.mkdir(parents=True)
        old_format = json.dumps({str(self.sub): {"sig": "x", "status": "ok", "note": "in sync"}})
        path.write_text("not json", encoding="utf-8")
        with self.assertLogs("subsync_repair", "WARNING"):
            self.assertTrue(self.main()[1][0].startswith("WOULD-FIX"))
        for content in ("[1, 2]", old_format):
            path.write_text(content, encoding="utf-8")
            with self.assertNoLogs("subsync_repair.engine.cache", "WARNING"):
                self.assertTrue(self.main()[1][0].startswith("WOULD-FIX"), content)

    def test_a_file_that_cannot_be_read_is_an_error_line_and_the_scan_goes_on(self):
        self.write("Show S01E02.mkv", b"")
        other = self.write("Show S01E02.ko.srt", srt_of(STARTS, KOREAN))
        real = batch.verdict_key

        def unreadable_first(sub, entries):
            if same_file(sub, self.sub):
                raise PermissionError(13, "Permission denied")
            return real(sub, entries)

        with mock.patch.object(batch, "verdict_key", unreadable_first):
            code, lines = self.main("--run")
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith(f"ERROR     {shown('Show/Show S01E01.ko.srt')}  file error (Permission"))
        self.assertEqual(self.starts_of(other), [s + 5.0 for s in STARTS])

    def test_a_library_folder_that_does_not_exist_is_a_warning_and_the_others_are_scanned(self):
        self.write_config(libraries=[str(self.root), str(self.tmp / "missing")])
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code, lines = self.main()
        self.assertEqual((code, lines[-1]), (0, "summary: would-fix 1"))
        self.assertIn("does not exist", stderr.getvalue())

    def test_a_queued_entry_that_cannot_be_removed_is_tried_once_per_run(self):
        japanese = self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        HookQueue(self.data / cli.QUEUE_DIR).add(japanese)
        with mock.patch.object(HookQueue, "done"):
            code, lines = self.main("--run")
        self.assertEqual(code, 0)
        self.assertEqual(sum(line.startswith("fixed: ") for line in lines), 1)

    def test_an_ass_left_to_its_srt_twin_is_remembered_until_one_of_them_changes(self):
        ass_file = self.write("Show S01E01.ko.ass", ASS_HEADER + ass_events(STARTS, KOREAN))
        self.aligner.shift = 0.0
        self.main("--run")
        self.assertIn("fixed together", self.cached()[self.cache_key(ass_file)]["note"])
        self.assertEqual(self.main("--run")[1], ["summary: cached 2"])

    def test_a_library_reached_through_a_link_is_pruned_too(self):
        link = self.tmp / "library-link"
        make_link(link, self.root)
        self.write_config(libraries=[str(link)])
        other = self.write("Show S01E02.ko.srt", srt_of(STARTS, KOREAN))
        self.main("--run")
        other.unlink()
        self.main("--run")
        self.assertEqual(set(self.cached()), {self.cache_key(self.sub)})

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
    def test_a_folder_is_listed_again_after_a_repair_changed_it(self):
        self.sub.unlink()
        body = "".join(f"<SYNC Start={int(s * 1000)}><P Class=KRCC>{t}\n" for s, t in zip(STARTS, KOREAN))
        smi = self.write("Show S01E01.ko.smi", f"<SAMI><BODY>\n{body}</BODY></SAMI>\n".encode("cp949"))
        ssa = self.write("Show S01E01.ko.ssa", ASS_HEADER + ass_events([s + 3.0 for s in STARTS], KOREAN))
        code, lines = self.main("--run")
        self.assertEqual(code, 0, lines)
        self.assertFalse(smi.exists())
        self.assertEqual(lines[-2], "summary: fixed 2")
        self.assertIn(f"Dialogue: 0,{ass.ass_time(STARTS[1] + 8.0)},", ssa.read_text(encoding="utf-8"))

    def test_queued_files_left_by_a_hook_are_repaired_by_the_next_run(self):
        japanese = self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        HookQueue(self.data / cli.QUEUE_DIR).add(japanese)
        code, lines = self.main("--run")
        self.assertEqual(code, 0)
        self.assertEqual(self.starts_of(japanese), [s + 5.0 for s in STARTS])
        self.assertTrue(any(line.startswith(f"fixed: {japanese}") for line in lines))
        self.assertEqual(HookQueue(self.data / cli.QUEUE_DIR).entries(), [])


class SingleFiles(Command):
    """Paths on the command line, as Bazarr's post-processing hook passes them: subsync-repair.py --hook --log <file>
    <subtitle>. Any supported language is accepted inside a library; every file gets exactly one line."""

    def test_the_hook_repairs_in_place_and_prints_and_logs_one_line(self):
        code, lines = self.hook(self.sub)
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"fixed: {self.sub}  shifts 0:00 +5.0s [ref: embedded en]"))
        self.assertIn("[originals in ", lines[0])
        logged = self.logged()
        self.assertEqual(len(logged), 1)
        self.assertRegex(logged[0], TIMESTAMPED)
        self.assertEqual(TIMESTAMPED.sub("", logged[0], count=1), lines[0])
        self.assertEqual(self.starts_of(self.sub), [s + 5.0 for s in STARTS])

    def test_a_cached_fix_names_no_backup_folder_even_when_this_run_made_one(self):
        self.hook(self.sub)
        self.write("Show S01E02.mkv", b"")
        other = self.write("Show S01E02.ko.srt", srt_of(STARTS, KOREAN))
        code, lines = self.main("--run", str(other), str(self.sub))
        self.assertEqual(code, 0)
        self.assertIn("[originals in", lines[0])
        self.assertTrue(lines[1].startswith(f"fixed: {self.sub}") and lines[1].endswith("(cached)"), lines[1])
        self.assertNotIn("[originals in", lines[1])

    def test_an_ass_that_could_not_be_put_back_is_not_shifted_again_by_the_next_run(self):
        ass_file = self.write("Show S01E01.ko.ass", "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
                              "MarginR, MarginV, Effect, Text\n" + "".join(
                                  f"Dialogue: 0,0:{int(s) // 60:02d}:{int(s) % 60:02d}.00,0:{int(s) // 60:02d}:"
                                  f"{int(s) % 60:02d}.50,Default,,0,0,0,,{t}\n" for s, t in zip(STARTS, KOREAN)))
        real = fileio.atomic_write_bytes
        ass_writes = []

        def srt_and_restore_fail(path, data, **kwargs):
            if same_file(path, self.sub):
                raise PermissionError(13, "in use")
            if same_file(path, ass_file):
                ass_writes.append(data)
                if len(ass_writes) > 1:
                    raise PermissionError(13, "in use")
            return real(path, data, **kwargs)

        with mock.patch.object(writers, "atomic_write_bytes", srt_and_restore_fail):
            self.assertEqual(self.main("--run")[0], 1)
        shifted_once = ass_file.read_bytes()
        self.aligner.calls.clear()
        self.aligner.shift = 0.0                # measured on its own, the shifted .ass is now in sync
        self.main("--run")
        self.assertEqual([[c.start for c in work] for _, work in self.aligner.calls], [[s + 5.0 for s in STARTS]],
                         "only the .ass is checked again; the .srt keeps its error")
        self.assertEqual(ass_file.read_bytes(), shifted_once)

    def test_a_subtitle_in_another_language_than_the_target_is_repaired(self):
        japanese = self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        code, lines = self.hook(japanese)
        self.assertEqual((code, len(lines)), (0, 1))
        self.assertTrue(lines[0].startswith("fixed: "))
        self.assertEqual(self.starts_of(japanese), [s + 5.0 for s in STARTS])

    def test_refusals_and_in_sync_are_normal_outcomes(self):
        self.aligner.shift = 0.0
        self.assertEqual(self.hook(self.sub), (0, [f"ok: {self.sub}  in sync"]))
        forced = self.write("Show S01E01.ko.forced.srt", srt_of(STARTS, KOREAN))
        code, lines = self.hook(forced)
        self.assertEqual(code, 0)
        self.assertIn("not a subtitle this tool repairs", lines[0])
        self.assertEqual(len(self.logged()), 2)

    def test_a_subtitle_of_another_episode_is_reported_as_a_mismatch_first_and_remembered(self):
        starts = [200.0 + 10 * i for i in range(60)]
        self.sub.write_text(srt_of(starts, [f"안녕하세요 {i}" for i in range(60)]), encoding="utf-8")
        self.aligner.starts_fn = lambda found: [s + another_episode_shift(i, s, starts) for i, s in enumerate(found)]
        code, lines = self.hook(self.sub)
        self.assertEqual((code, len(lines)), (0, 1))
        self.assertTrue(lines[0].startswith(f"mismatch: {self.sub}  the subtitle does not match this video"))
        self.assertEqual(self.cached()[self.cache_key(self.sub)]["status"], "mismatch")
        self.assertEqual(self.hook(self.sub)[1][0][:len("mismatch: ")], "mismatch: ")
        self.assertIn("(cached)", self.hook(self.sub)[1][0])

    def test_a_path_outside_the_libraries_is_refused_without_looking_at_it(self):
        outside = self.write("x.ko.srt", srt_of(STARTS, KOREAN), folder=self.tmp / "elsewhere")
        code, lines = self.hook(outside)
        self.assertEqual((code, lines), (0, [f"skip: {outside}  not in a configured library"]))
        self.assertEqual(self.aligner.calls, [])

    def test_a_missing_file_and_a_folder_are_errors(self):
        code, lines = self.hook(self.dir / "Show S01E09.ko.srt")
        self.assertEqual(code, 1)
        self.assertIn("no such file", lines[0])
        code, lines = self.hook(self.dir)
        self.assertEqual((code, lines), (1, [f"error: {self.dir}  not a file"]))

    def test_two_files_get_two_lines_and_one_file_named_twice_one(self):
        other = self.write("Show S01E02.ko.srt", srt_of(STARTS, KOREAN))
        code, lines = self.main("--run", str(self.sub), str(other))
        self.assertEqual((code, len(lines)), (0, 2))
        spelled_otherwise = self.dir / "." / self.sub.name
        code, lines = self.main(str(self.sub), str(spelled_otherwise))
        self.assertEqual((code, len(lines)), (0, 1))

    def test_the_hook_leaves_a_smi_in_place(self):
        smi = self.write("Show S01E01.ko.smi", b"<SAMI><BODY></BODY></SAMI>")
        code, lines = self.hook(smi)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [f"skip: {smi}  the hook leaves a .smi in place; a scan or a manual run converts it "
                                 f"to .srt"])
        self.assertTrue(smi.exists())

    def test_a_second_run_waits_for_the_first_to_finish(self):
        lock = fileio.process_lock(self.data / cli.LOCK_FILE)
        lock.__enter__()
        threading.Timer(0.5, lock.__exit__, (None, None, None)).start()
        started = time.monotonic()
        with mock.patch.object(fileio, "LOCK_POLL", 0.05):
            code, lines = self.main("--run", str(self.sub))
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.4)
        self.assertLess(waited, fileio.LOCK_POLL + 0.4, "the shorter poll interval was used")
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("fixed: "))

    def test_a_run_that_waits_too_long_gives_up_with_one_line_per_file_and_changes_nothing(self):
        other = self.write("Show S01E02.ko.srt", srt_of(STARTS, KOREAN))
        before = self.sub.read_bytes()
        lock_file = self.data / cli.LOCK_FILE
        with fileio.process_lock(lock_file), mock.patch.object(cli, "LOCK_WAIT", 0.2):
            code, lines = self.main("--run", str(self.sub), str(other))
        self.assertEqual(code, 1)
        self.assertEqual(lines, [f"error: {path}  another repair is still running [another run held {lock_file} for "
                                 f"0.2 s]" for path in (self.sub, other)])
        self.assertEqual(self.sub.read_bytes(), before)

    def test_a_hook_that_cannot_get_the_lock_queues_its_file_for_the_run_that_holds_it(self):
        before = self.sub.read_bytes()
        with fileio.process_lock(self.data / cli.LOCK_FILE), mock.patch.object(cli, "HOOK_LOCK_WAIT", 0.2):
            code, lines = self.hook(self.sub)
        self.assertEqual((code, lines), (0, [f"queued: {self.sub}  {cli.QUEUED}"]))
        self.assertEqual(self.sub.read_bytes(), before)
        self.assertEqual([path for _, path in HookQueue(self.data / cli.QUEUE_DIR).entries()],
                         [str(os.path.realpath(self.sub))])

    def test_only_the_hook_flag_makes_a_run_the_hook(self):
        lock_file = self.data / cli.LOCK_FILE
        with fileio.process_lock(lock_file), mock.patch.object(cli, "LOCK_WAIT", 0.2), \
                mock.patch.object(cli, "HOOK_LOCK_WAIT", 0.2):
            code, lines = self.main("--run", "--log", str(self.log), str(self.sub))
        self.assertEqual((code, lines[0][:len("error: ")]), (1, "error: "), "a manual run is not queued")
        self.assertEqual(HookQueue(self.data / cli.QUEUE_DIR).entries(), [])

    def test_a_hook_without_a_log_still_prints_one_line(self):
        japanese = self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        HookQueue(self.data / cli.QUEUE_DIR).add(os.path.realpath(japanese))
        code, lines = self.main("--hook", str(self.sub))
        self.assertEqual((code, len(lines)), (0, 1))
        self.assertEqual(self.starts_of(japanese), [s + 5.0 for s in STARTS])

    def test_the_hook_needs_files_and_does_not_combine_with_json(self):
        for argv in (("--hook",), ("--hook", "--json", str(self.sub))):
            with self.assertRaises(SystemExit) as raised, mock.patch("sys.stderr"):
                self.main(*argv)
            self.assertEqual(raised.exception.code, 2, argv)

    def test_the_run_that_holds_the_lock_repairs_what_hooks_queued_meanwhile(self):
        japanese = self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        real = Repairer.repair

        def repair_while_a_hook_queues(repairer, sub, *args, **kwargs):
            if same_file(sub, self.sub):
                HookQueue(self.data / cli.QUEUE_DIR).add(os.path.realpath(japanese))
            return real(repairer, sub, *args, **kwargs)

        with mock.patch.object(Repairer, "repair", repair_while_a_hook_queues):
            code, lines = self.hook(self.sub)
        self.assertEqual((code, len(lines)), (0, 1), "the queued file's line goes to the log only")
        self.assertEqual(self.starts_of(japanese), [s + 5.0 for s in STARTS])
        self.assertTrue(any(f"fixed: {os.path.realpath(japanese)}" in line for line in self.logged()))
        self.assertEqual(HookQueue(self.data / cli.QUEUE_DIR).entries(), [])

    def test_a_hook_that_finds_the_lock_free_after_queueing_repairs_the_queue_itself(self):
        queue = HookQueue(self.data / cli.QUEUE_DIR)
        real_add = HookQueue.add

        def add_then_release(hook_queue, path):
            real_add(hook_queue, path)
            holder.__exit__(None, None, None)

        holder = fileio.process_lock(self.data / cli.LOCK_FILE)
        holder.__enter__()
        with mock.patch.object(cli, "HOOK_LOCK_WAIT", 0.1), mock.patch.object(HookQueue, "add", add_then_release):
            code, lines = self.hook(self.sub)
        self.assertEqual((code, lines), (0, [f"queued: {self.sub}  {cli.QUEUED}"]))
        self.assertEqual(self.starts_of(self.sub), [s + 5.0 for s in STARTS])
        self.assertEqual(queue.entries(), [])

    def test_a_missing_broken_or_non_utf8_config_is_one_line_and_exit_code_one(self):
        for content in (None, "{not json", "[]", json.dumps({"libraries": [str(self.root)]}),
                        "{\"target_language\": \"ko\", \"libraries\": [\"C:\\Media\"]}"):
            if content is None:
                self.config.unlink(missing_ok=True)
            else:
                self.config.write_text(content, encoding="utf-8")
            code, lines = self.hook(self.sub)
            self.assertEqual((code, len(lines)), (1, 1), content)
            self.assertTrue(lines[0].startswith("error: "))
        self.config.write_text(json.dumps({"target_language": "ko", "libraries": [str(self.root)]}), encoding="utf-16")
        code, lines = self.hook(self.sub)
        self.assertEqual((code, len(lines)), (1, 1))
        self.assertIn("must be saved as UTF-8", lines[0])

    def test_a_data_folder_that_cannot_be_used_is_one_line(self):
        with mock.patch.object(cli.datadir, "prepare", side_effect=PermissionError(13, "Access is denied")):
            code, lines = self.hook(self.sub)
        self.assertEqual((code, len(lines)), (1, 1))
        self.assertIn("Access is denied", lines[0])

    def test_a_log_that_cannot_be_written_is_one_line(self):
        blocker = self.write("not-a-folder", b"")
        code, lines = self.main("--run", "--log", str(blocker / "x.log"), str(self.sub))
        self.assertEqual((code, len(lines)), (1, 1))
        self.assertTrue(lines[0].startswith("error: cannot write the log"))

    def test_with_a_log_file_warnings_go_there_in_the_same_format_and_standard_error_stays_empty(self):
        self.write_config(unused_setting={"a": 1})
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code, lines = self.hook(self.sub)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        logged = "\n".join(self.logged())
        self.assertRegex(logged, STAMP + r"WARNING subsync_repair\.cli: .*'unused_setting' is not used")
        self.assertIn(lines[0], logged)

    def test_python_warnings_go_to_the_log_file_too(self):
        real = Repairer.repair

        def warn_then_repair(*args, **kwargs):
            warnings.warn("a library is old", DeprecationWarning)
            return real(*args, **kwargs)

        with warnings.catch_warnings(), mock.patch.object(Repairer, "repair", warn_then_repair):
            warnings.simplefilter("always")
            self.hook(self.sub)
        self.assertIn("a library is old", "\n".join(self.logged()))

    def test_without_a_log_file_warnings_go_to_standard_error(self):
        self.write_config(unused_setting={"a": 1})
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.main(str(self.sub))
        self.assertIn("'unused_setting' is not used", stderr.getvalue())

    def test_on_windows_piped_output_is_ascii_so_any_code_page_reads_it(self):
        self.write("Ünïcödé S01E01.mkv", b"")
        japanese = self.write("Ünïcödé S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        raw = io.BytesIO()
        piped = io.TextIOWrapper(raw, encoding="utf-8")
        with mock.patch.object(cli, "Repairer", self.repairer_for), mock.patch("sys.stdout", piped), \
                mock.patch.object(cli, "WINDOWS", True):
            cli.main(["--json", str(japanese), "--config", str(self.config)])
            cli.main([str(japanese), "--config", str(self.config)])
            piped.flush()
        output = raw.getvalue()
        self.assertTrue(output.isascii())
        result, line = output.decode("ascii").splitlines()
        self.assertEqual(json.loads(result)["path"], str(japanese))
        self.assertTrue(line.startswith("would-fix: "))

    def test_elsewhere_piped_output_stays_utf8(self):
        raw = io.BytesIO()
        piped = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
        cli.plain_output(piped, windows=False)
        print("Ünïcödé", file=piped)
        piped.flush()
        self.assertEqual(raw.getvalue(), "Ünïcödé\n".encode("utf-8"))


class Json(Command):
    def test_json_prints_one_result_and_the_exit_code_reflects_errors(self):
        code, lines = self.main("--json", str(self.sub))
        self.assertEqual(code, 0)
        result = json.loads(lines[0])
        self.assertEqual((result["status"], result["output"], result["shifts"], result["reference"]),
                         ("would-fix", None, [[0.0, 5.0]], "embedded en"))
        self.aligner.fail = ("alass failed", "")
        self.assertEqual(self.main("--json", str(self.sub))[0], 1)

    def test_a_refused_or_missing_path_is_json_too(self):
        outside = self.write("x.ko.srt", srt_of(STARTS, KOREAN), folder=self.tmp / "elsewhere")
        code, lines = self.main("--json", str(outside))
        self.assertEqual((code, json.loads(lines[0])["status"]), (0, "skip"))
        code, lines = self.main("--json", str(self.dir / "gone.ko.srt"))
        self.assertEqual((code, json.loads(lines[0])["note"]), (1, "no such file"))

    def test_out_writes_a_new_file(self):
        dest = self.dir / "new.srt"
        code, lines = self.main("--json", "--out", str(dest), str(self.sub))
        self.assertEqual((code, json.loads(lines[0])["output"]), (0, str(dest)))
        self.assertEqual(self.starts_of(dest), [s + 5.0 for s in STARTS])

    def test_wrong_option_combinations_are_usage_errors(self):
        for argv in (("--json",), ("--json", str(self.sub), str(self.sub)), ("--json", "--run", str(self.sub)),
                     ("--out", "x.srt", str(self.sub)), ("--json", "--out", "x.ass", str(self.sub))):
            with self.assertRaises(SystemExit) as raised, mock.patch("sys.stderr"):
                self.main(*argv)
            self.assertEqual(raised.exception.code, 2, argv)


class Fingerprint(LibraryCase):
    def test_every_input_of_a_verdict_changes_the_fingerprint(self):
        base = cache.fingerprint(self.settings)
        tools = self.settings.tools
        changes = {
            "embedded order": dataclasses.replace(self.settings, embedded_order=("ja",)),
            "sibling order": dataclasses.replace(self.settings, sibling_order=("ja",)),
            "alass": dataclasses.replace(self.settings, tools=dataclasses.replace(tools, alass="/opt/other-alass")),
            "ffmpeg": dataclasses.replace(self.settings, tools=dataclasses.replace(tools, ffmpeg="/opt/ffmpeg")),
            "ffprobe": dataclasses.replace(self.settings, tools=dataclasses.replace(tools, ffprobe="/opt/ffprobe")),
            "ffsubsync": dataclasses.replace(self.settings, tools=dataclasses.replace(tools, ffsubsync_python="py")),
            "ffsubsync libs": dataclasses.replace(self.settings, tools=dataclasses.replace(tools,
                                                                                            ffsubsync_libs=("/l",))),
        }
        for what, settings in changes.items():
            with self.subTest(what):
                self.assertNotEqual(cache.fingerprint(settings), base)
        with self.subTest("version"), mock.patch.object(cache, "__version__", __version__ + ".1"):
            self.assertNotEqual(cache.fingerprint(self.settings), base)
        with self.subTest("code"), mock.patch.object(cache, "code_hash", lambda: "other code"):
            self.assertNotEqual(cache.fingerprint(self.settings), base)
        with self.subTest("a new build of a tool"), mock.patch.object(programs, "identity",
                                                                      lambda command: f"{command}: 2"):
            self.assertNotEqual(cache.fingerprint(self.settings), base)
        self.assertEqual(cache.fingerprint(self.settings), base)

    def test_the_code_hash_covers_the_rule_modules(self):
        sources = {path.resolve() for path in cache.verdict_sources()}
        for module in (policy, references, ass, audio_check):
            self.assertIn(Path(module.__file__).resolve(), sources)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are not installed")
class EndToEnd(Command):
    """The hook command against a real video with an English track (made by ffmpeg); only alass is faked."""

    def setUp(self):
        super().setUp()
        reference = self.write("reference.srt", srt_of([c.start for c in track()], ENGLISH), folder=self.tmp)
        self.video.unlink()
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(reference), "-c:s", "srt",
                        "-metadata:s:s:0", "language=eng", str(self.video)], check=True, timeout=60)
        self.sub.unlink()
        self.sub = self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))

    def repairer_for(self, settings):
        return Repairer(settings, Media(), self.aligner, FakeFfsubsync())

    def test_the_hook_finds_the_track_in_the_video_and_repairs_the_subtitle(self):
        code, lines = self.hook(self.sub)
        self.assertEqual((code, len(lines)), (0, 1))
        self.assertIn("[ref: embedded en]", lines[0])
        self.assertEqual(self.starts_of(self.sub), [s + 5.0 for s in STARTS])
        reference, _ = self.aligner.calls[0]
        self.assertEqual(reference[0].text, ENGLISH[0])


class Script(unittest.TestCase):
    def test_the_script_prints_its_help_and_the_serve_subcommand_its_own(self):
        for argv, expected in (([], "subsync-repair.py --hook --log FILE SUBTITLE"), (["serve"], "--port PORT")):
            r = subprocess.run([sys.executable, str(ROOT / "subsync-repair.py"), *argv, "--help"], capture_output=True,
                               text=True, encoding="utf-8", timeout=60, check=True)
            self.assertIn(expected, r.stdout)


if __name__ == "__main__":
    unittest.main()
