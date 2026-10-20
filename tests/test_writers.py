"""Tests for the backups in subsync_repair/engine/writers.py."""
import datetime
import os
import unittest
from pathlib import Path
from unittest import mock

from subsync_repair.engine.writers import Backups
from support import TempFolder

NOON = datetime.datetime(2026, 1, 2, 12, 0, 0)


class BackupCopies(TempFolder):
    def setUp(self):
        super().setUp()
        self.backups = Backups(self.tmp / "bk", now=lambda: NOON)

    def file(self, relative, content="x"):
        p = self.tmp / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_nothing_is_made_until_something_is_backed_up(self):
        self.assertIsNone(self.backups.folder)
        self.assertFalse((self.tmp / "bk").exists())

    def test_each_run_gets_its_own_folder_even_within_the_same_second(self):
        f = self.file("ep.ko.srt")
        second = Backups(self.tmp / "bk", now=lambda: NOON)
        first_copy, second_copy = self.backups.keep(f, b"first"), second.keep(f, b"second")
        self.assertEqual((self.backups.folder.name, second.folder.name), ("repair-20260102-120000",
                                                                          "repair-20260102-120000-2"))
        self.assertEqual((first_copy.read_bytes(), second_copy.read_bytes()), (b"first", b"second"))

    def test_same_named_files_in_different_folders_do_not_collide(self):
        a, b = self.file("a/S1/ep.ko.srt", "A"), self.file("b/S1/ep.ko.srt", "B")
        da, db = self.backups.keep(a, b"A"), self.backups.keep(b, b"B")
        self.assertNotEqual(da, db)
        self.assertEqual((da.read_text(), db.read_text()), ("A", "B"))

    def test_the_same_bytes_are_kept_once_and_other_bytes_of_the_same_file_next_to_them(self):
        f = self.file("ep.ko.srt", "original")
        first = self.backups.keep(f, b"original")
        self.assertEqual(self.backups.keep(f, b"original"), first)
        later = self.backups.keep(f, b"changed")
        self.assertEqual((first.read_bytes(), later.read_bytes(), later.name), (b"original", b"changed",
                                                                                 "ep.ko.srt.~2~"))
        self.assertEqual(self.backups.keep(f, b"changed"), later)

    def test_the_backup_keeps_the_modification_time(self):
        f = self.file("ep.ko.srt")
        os.utime(f, (1_000_000_000, 1_000_000_000))
        self.assertEqual(int(self.backups.keep(f, b"x").stat().st_mtime), 1_000_000_000)

    def test_a_relative_path_is_made_absolute_instead_of_becoming_the_backup_folder(self):
        self.file("ep.ko.srt", "R")
        cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            dest = self.backups.keep(Path("ep.ko.srt"), b"R")
        finally:
            os.chdir(cwd)
        self.assertEqual((dest.read_text(), dest.name), ("R", "ep.ko.srt"))
        self.assertTrue(dest.is_relative_to(self.backups.folder))

    @unittest.skipUnless(os.name == "nt", "drive letters exist on Windows only")
    def test_the_drive_is_part_of_the_backup_path(self):
        f = self.file("ep.ko.srt")
        self.assertEqual(self.backups.keep(f, b"x").relative_to(self.backups.folder).parts[0],
                         Path(os.path.abspath(f)).drive.replace(":", ""))

    @unittest.skipUnless(os.name == "nt", "UNC paths exist on Windows only")
    def test_a_network_share_becomes_a_folder_without_contacting_its_host(self):
        touched = mock.Mock(side_effect=AssertionError("the network was asked"))
        with mock.patch("os.path.realpath", touched), mock.patch.object(Path, "resolve", touched):
            dest = self.backups.path_for("\\\\media-host\\share\\Show\\ep.ko.srt")
        self.assertEqual(dest.relative_to(self.backups.folder).parts, ("media-host_share", "Show", "ep.ko.srt"))

    def test_a_crash_while_copying_leaves_no_backup_that_looks_complete(self):
        f = self.file("ep.ko.srt", "original")
        with mock.patch("subsync_repair.storage.fileio.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.backups.keep(f, b"original")
        self.assertFalse(any(p.is_file() for p in self.backups.folder.rglob("*")))
        self.assertEqual(self.backups.keep(f, b"original").read_text(), "original")


if __name__ == "__main__":
    unittest.main()
