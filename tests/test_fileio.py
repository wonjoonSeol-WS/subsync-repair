"""Tests for subsync_repair/storage/fileio.py: atomic writes, leftovers of interrupted writes, and the lock between
processes."""
import errno
import os
import re
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from subsync_repair.config import ROOT
from subsync_repair.storage import fileio
from support import TempFolder, make_link

POSIX_ONLY = unittest.skipIf(os.name == "nt", "file modes, owners and folder fsync are POSIX notions")


class AtomicWrite(TempFolder):
    def names(self):
        return sorted(p.name for p in self.tmp.iterdir())

    def test_a_new_file_is_created_with_the_bytes_and_no_temp_file_is_left(self):
        fileio.atomic_write_bytes(self.tmp / "a.srt", "안녕\r\n".encode("utf-8"))
        self.assertEqual((self.tmp / "a.srt").read_bytes(), "안녕\r\n".encode("utf-8"))
        self.assertEqual(self.names(), ["a.srt"])

    def test_an_existing_file_is_replaced(self):
        (self.tmp / "a.srt").write_text("old")
        fileio.atomic_write_bytes(self.tmp / "a.srt", b"new")
        self.assertEqual((self.tmp / "a.srt").read_bytes(), b"new")

    def test_the_temporary_name_is_short_and_recognisable(self):
        seen = []
        real = fileio.create_temp

        def record(folder):
            fd, path = real(folder)
            seen.append(path.name)
            return fd, path

        with mock.patch.object(fileio, "create_temp", record):
            fileio.atomic_write_bytes(self.tmp / ("x" * 100 + ".srt"), b"1")
        self.assertLess(len(seen[0]), 20)
        self.assertRegex(seen[0], fileio.TEMP_NAME)

    def test_a_failing_replace_keeps_the_original_and_removes_the_temp_file(self):
        (self.tmp / "a.srt").write_text("original")
        with mock.patch.object(fileio.os, "replace", side_effect=PermissionError("in use")):
            with self.assertRaises(PermissionError):
                fileio.atomic_write_bytes(self.tmp / "a.srt", b"new")
        self.assertEqual((self.tmp / "a.srt").read_text(), "original")
        self.assertEqual(self.names(), ["a.srt"])

    def test_a_failing_write_leaves_nothing_behind(self):
        with mock.patch.object(fileio.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fileio.atomic_write_bytes(self.tmp / "new.srt", b"data")
        self.assertEqual(self.names(), [])

    def test_a_read_only_file_is_refused_and_nothing_is_left_behind(self):
        target = self.tmp / "a.srt"
        target.write_text("keep me")
        os.chmod(target, stat.S_IREAD)
        self.addCleanup(os.chmod, target, stat.S_IREAD | stat.S_IWRITE)
        with self.assertRaises(PermissionError) as raised:
            fileio.atomic_write_bytes(target, b"new")
        self.assertIn("read-only", str(raised.exception))
        self.assertEqual(target.read_text(), "keep me")
        self.assertEqual(self.names(), ["a.srt"])

    def test_a_temp_file_that_cannot_be_removed_neither_hides_the_error_nor_fails_a_good_write(self):
        real_unlink = Path.unlink

        def stuck(path, *args, **kwargs):
            if path.name.endswith(fileio.TEMP_SUFFIX):
                raise PermissionError(errno.EACCES, "in use")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", stuck), self.assertLogs("subsync_repair.storage.fileio", "WARNING"):
            fileio.atomic_write_bytes(self.tmp / "new.srt", b"data", exclusive=True)
        self.assertEqual((self.tmp / "new.srt").read_bytes(), b"data")
        (self.tmp / "a.srt").write_text("original")
        with mock.patch.object(Path, "unlink", stuck), mock.patch.object(fileio.os, "replace",
                                                                         side_effect=OSError(errno.EIO, "io")), \
                self.assertLogs("subsync_repair.storage.fileio", "WARNING"):
            with self.assertRaisesRegex(OSError, "io"):
                fileio.atomic_write_bytes(self.tmp / "a.srt", b"new")

    def test_exclusive_never_replaces_an_existing_file(self):
        (self.tmp / "a.srt").write_text("keep me")
        with self.assertRaises(FileExistsError):
            fileio.atomic_write_bytes(self.tmp / "a.srt", b"new", exclusive=True)
        self.assertEqual((self.tmp / "a.srt").read_text(), "keep me")
        fileio.atomic_write_bytes(self.tmp / "b.srt", b"fresh", exclusive=True)
        self.assertEqual(self.names(), ["a.srt", "b.srt"])

    def test_exclusive_reports_an_existing_read_only_file_as_existing(self):
        target = self.tmp / "a.srt"
        target.write_text("keep me")
        os.chmod(target, stat.S_IREAD)
        self.addCleanup(os.chmod, target, stat.S_IREAD | stat.S_IWRITE)
        with self.assertRaises(FileExistsError):
            fileio.atomic_write_bytes(target, b"new", exclusive=True)

    def test_exclusive_works_without_hard_links_and_still_refuses_an_existing_file(self):
        with mock.patch.object(fileio.os, "link", side_effect=OSError(errno.EPERM, "no hard links")):
            fileio.atomic_write_bytes(self.tmp / "a.srt", b"fresh", exclusive=True)
            self.assertEqual((self.tmp / "a.srt").read_bytes(), b"fresh")
            with self.assertRaises(FileExistsError):
                fileio.atomic_write_bytes(self.tmp / "a.srt", b"new", exclusive=True)
        self.assertEqual((self.tmp / "a.srt").read_bytes(), b"fresh")
        self.assertEqual(self.names(), ["a.srt"])

    def test_a_claimed_name_is_given_back_when_the_move_fails(self):
        tmp = self.tmp / "t.tmp"
        tmp.write_bytes(b"new")
        with mock.patch.object(fileio.os, "replace", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(OSError):
                fileio.claim_and_replace(tmp, self.tmp / "a.srt")
        self.assertFalse((self.tmp / "a.srt").exists())
        fileio.claim_and_replace(tmp, self.tmp / "a.srt")
        self.assertEqual((self.tmp / "a.srt").read_bytes(), b"new")

    def test_a_writer_that_loses_the_race_for_a_new_name_gets_file_exists(self):
        real_link = os.link

        def someone_else_first(src, dst):
            Path(dst).write_bytes(b"theirs")
            return real_link(src, dst)

        with mock.patch.object(fileio.os, "link", someone_else_first):
            with self.assertRaises(FileExistsError):
                fileio.atomic_write_bytes(self.tmp / "a.srt", b"mine", exclusive=True)
        self.assertEqual((self.tmp / "a.srt").read_bytes(), b"theirs")
        self.assertEqual(self.names(), ["a.srt"])

    def test_a_link_is_refused_when_links_must_not_be_followed(self):
        target = self.tmp / "target"
        target.mkdir()
        make_link(self.tmp / "dest.srt", target)
        with self.assertRaisesRegex(PermissionError, "is a link"):
            fileio.atomic_write_bytes(self.tmp / "dest.srt", b"x", exclusive=True, follow_links=False)
        self.assertEqual(list(target.iterdir()), [])

    def test_a_new_file_made_like_a_read_only_one_is_read_only_too(self):
        source = self.tmp / "a.smi"
        source.write_text("x")
        os.chmod(source, stat.S_IREAD)
        self.addCleanup(os.chmod, source, stat.S_IREAD | stat.S_IWRITE)
        fileio.atomic_write_bytes(self.tmp / "a.srt", b"new", exclusive=True, like=source)
        self.addCleanup(os.chmod, self.tmp / "a.srt", stat.S_IREAD | stat.S_IWRITE)
        self.assertFalse((self.tmp / "a.srt").stat().st_mode & stat.S_IWUSR)

    @POSIX_ONLY
    def test_modes_are_kept_and_the_folder_is_synced(self):
        (self.tmp / "a.srt").write_text("old")
        (self.tmp / "a.srt").chmod(0o664)
        with mock.patch.object(fileio.os, "fsync", wraps=os.fsync) as fsync:
            fileio.atomic_write_bytes(self.tmp / "a.srt", b"new")
        self.assertEqual(stat.S_IMODE((self.tmp / "a.srt").stat().st_mode), 0o664)
        self.assertEqual(fsync.call_count, 2)

    @POSIX_ONLY
    def test_a_new_file_follows_the_umask_or_the_file_it_replaces(self):
        old = os.umask(0o027)
        self.addCleanup(os.umask, old)
        fileio.atomic_write_bytes(self.tmp / "b.srt", b"new")
        self.assertEqual(stat.S_IMODE((self.tmp / "b.srt").stat().st_mode), 0o640)
        source = self.tmp / "a.smi"
        source.write_text("x")
        source.chmod(0o604)
        fileio.atomic_write_bytes(self.tmp / "a.srt", b"new", exclusive=True, like=source)
        self.assertEqual(stat.S_IMODE((self.tmp / "a.srt").stat().st_mode), 0o604)

    @POSIX_ONLY
    def test_the_group_of_the_replaced_file_is_kept_where_allowed(self):
        (self.tmp / "a.srt").write_text("old")
        st = (self.tmp / "a.srt").stat()
        with mock.patch.object(fileio.os, "chown") as chown:
            fileio.atomic_write_bytes(self.tmp / "a.srt", b"new")
        owner = st.st_uid if os.geteuid() == 0 else -1
        self.assertEqual(chown.call_args.args[1:], (owner, st.st_gid))

    @POSIX_ONLY
    def test_a_symlinked_subtitle_is_written_through_to_its_target(self):
        target = self.tmp / "real.srt"
        target.write_text("old")
        link = self.tmp / "link.srt"
        link.symlink_to(target)
        fileio.atomic_write_bytes(link, b"new")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b"new")

    def test_a_signature_changes_with_the_file(self):
        f = self.tmp / "a.srt"
        f.write_text("one")
        first = fileio.signature(f)
        f.write_text("three")
        self.assertNotEqual(fileio.signature(f), first)


class Leftovers(TempFolder):
    def leave(self, name, content, age):
        path = self.tmp / name
        path.write_bytes(content)
        then = time.time() - age
        os.utime(path, (then, then))
        return path

    def test_old_temporary_files_and_claims_of_this_tool_are_removed_and_nothing_else(self):
        day = fileio.STALE_AFTER
        old_temp = self.leave(".ssr-0123abcd.tmp", b"half", day + 60)
        old_claim = self.leave("Show.ko.srt", fileio.CLAIM, day + 60)
        fresh_temp = self.leave(".ssr-89abcdef.tmp", b"half", 60)
        fresh_claim = self.leave("Show.en.srt", fileio.CLAIM, 60)
        foreign = self.leave(".other.tmp", b"x", day + 60)
        same_size = self.leave("Show.ja.srt", b"x" * len(fileio.CLAIM), day + 60)
        fileio.sweep_stale(self.tmp)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         sorted(p.name for p in (fresh_temp, fresh_claim, foreign, same_size)))
        self.assertFalse(old_temp.exists() or old_claim.exists())

    def test_a_folder_is_swept_once_per_process(self):
        fileio.sweep_stale(self.tmp)
        late = self.leave(".ssr-0123abcd.tmp", b"half", fileio.STALE_AFTER + 60)
        fileio.sweep_stale(self.tmp)
        self.assertTrue(late.exists())


class Links(TempFolder):
    def test_a_folder_link_is_a_link_and_a_folder_is_not(self):
        target = self.tmp / "target"
        target.mkdir()
        make_link(self.tmp / "link", target)
        self.assertTrue(fileio.is_link(self.tmp / "link"))
        self.assertFalse(fileio.is_link(target))
        self.assertFalse(fileio.is_link(self.tmp / "missing"))


class ProcessLock(TempFolder):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "data" / "run.lock"

    def test_a_second_holder_gives_up_after_the_wait(self):
        with fileio.process_lock(self.path):
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, re.escape(f"held {self.path} for 0.2 s")):
                with fileio.process_lock(self.path, wait=0.2, poll=0.05):
                    pass
            self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_a_waiting_holder_blocks_until_the_lock_is_released(self):
        first = fileio.process_lock(self.path)
        first.__enter__()
        threading.Timer(0.3, first.__exit__, (None, None, None)).start()
        started = time.monotonic()
        with fileio.process_lock(self.path, wait=5, poll=0.02):
            waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.25)

    def test_the_poll_interval_is_read_when_the_lock_is_taken(self):
        first = fileio.process_lock(self.path)
        first.__enter__()
        threading.Timer(0.1, first.__exit__, (None, None, None)).start()
        started = time.monotonic()
        with mock.patch.object(fileio, "LOCK_POLL", 0.02), fileio.process_lock(self.path, wait=5):
            waited = time.monotonic() - started
        self.assertLess(waited, 0.5)

    def test_another_process_holding_the_lock_keeps_it_from_this_one(self):
        code = ("import sys, time\nfrom subsync_repair.storage import fileio\n"
                "with fileio.process_lock(sys.argv[1]):\n    print('held', flush=True)\n    sys.stdin.readline()\n")
        holder = subprocess.Popen([sys.executable, "-c", code, str(self.path)], cwd=ROOT, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait, 30)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with self.assertRaises(TimeoutError):
                with fileio.process_lock(self.path, wait=0.1, poll=0.02):
                    pass
        finally:
            holder.stdin.close()
            holder.stdout.close()
        holder.wait(30)
        with fileio.process_lock(self.path, wait=5, poll=0.02):
            pass

    def test_the_lock_can_be_taken_again_after_release(self):
        with fileio.process_lock(self.path):
            pass
        with fileio.process_lock(self.path):
            pass


if __name__ == "__main__":
    unittest.main()
