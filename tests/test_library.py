"""Tests for subsync_repair/storage/library.py: which library holds a path, and walking a library."""
import os
import unittest
from pathlib import Path
from unittest import mock

from subsync_repair.storage import library
from subsync_repair.storage.library import Libraries, lexical_key, walk
from support import TempFolder, make_link


class Keys(unittest.TestCase):
    def test_letter_case_is_ignored_where_the_file_system_ignores_it(self):
        with mock.patch.object(library, "CASE_INSENSITIVE", True):
            self.assertEqual(lexical_key("/Media/Show"), lexical_key("/media/SHOW"))
        with mock.patch.object(library, "CASE_INSENSITIVE", False), mock.patch.object(library.os.path, "normcase",
                                                                                      lambda path: path):
            self.assertNotEqual(lexical_key("/Media/Show"), lexical_key("/media/SHOW"))

    def test_the_platforms_that_ignore_case(self):
        self.assertEqual(library.CASE_INSENSITIVE, library.sys.platform in ("win32", "darwin"))


class Walk(TempFolder):
    def make(self, *relative):
        for name in relative:
            path = self.tmp / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")

    def test_every_folder_top_down_in_name_order_with_its_sorted_files(self):
        self.make("lib/b/x.srt", "lib/a/y.srt", "lib/a/z/w.srt", "lib/top.srt", "lib/Extras/e.srt")
        found = [(str(folder.relative_to(self.tmp / "lib")), [f.name for f in files])
                 for folder, files in walk(self.tmp / "lib", {"extras"})]
        self.assertEqual(found, [(".", ["top.srt"]), ("a", ["y.srt"]), (os.path.join("a", "z"), ["w.srt"]),
                                 ("b", ["x.srt"])])

    def test_a_link_to_another_folder_is_never_entered(self):
        self.make("lib/Show/a.ko.srt", "outside/Hidden/b.ko.srt")
        make_link(self.tmp / "lib" / "Linked", self.tmp / "outside")
        names = [f.name for _, files in walk(self.tmp / "lib") for f in files]
        self.assertEqual(names, ["a.ko.srt"])

    def test_a_library_folder_that_is_itself_a_link_is_walked(self):
        self.make("real/Show/a.ko.srt")
        make_link(self.tmp / "lib", self.tmp / "real")
        self.assertEqual([f.name for _, files in walk(self.tmp / "lib") for f in files], ["a.ko.srt"])

    def test_an_unreadable_folder_has_no_files(self):
        self.assertEqual(list(walk(self.tmp / "missing")), [(self.tmp / "missing", [])])


class Resolve(TempFolder):
    def test_the_library_folders_are_followed_once_not_on_every_lookup(self):
        (self.tmp / "lib").mkdir()
        libraries = Libraries([self.tmp / "lib", self.tmp / "other"])
        with mock.patch.object(library.os.path, "realpath", wraps=os.path.realpath) as realpath:
            libraries.resolve(str(self.tmp / "lib" / "a.srt"))
        self.assertEqual(realpath.call_count, 1)

    def test_a_path_must_be_inside_a_library_as_written_and_after_links(self):
        (self.tmp / "lib" / "Show").mkdir(parents=True)
        (self.tmp / "outside").mkdir()
        (self.tmp / "outside" / "x.srt").write_bytes(b"")
        make_link(self.tmp / "lib" / "Linked", self.tmp / "outside")
        libraries = Libraries([self.tmp / "lib"])
        self.assertEqual(libraries.resolve(str(self.tmp / "lib" / "Show" / "a.srt")),
                         Path(os.path.realpath(self.tmp / "lib" / "Show" / "a.srt")))
        self.assertIsNone(libraries.resolve(str(self.tmp / "lib" / "Linked" / "x.srt")))
        self.assertIsNone(libraries.resolve(str(self.tmp / "outside" / "x.srt")))
        self.assertEqual(libraries.containing(str(self.tmp / "lib" / "Show")), self.tmp / "lib")


if __name__ == "__main__":
    unittest.main()
