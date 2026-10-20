"""Tests for subsync_repair/external/programs.py: finding the external programs."""
import os
import unittest
from pathlib import Path
from unittest import mock

from subsync_repair.external import programs
from support import TempFolder


class Available(TempFolder):
    def test_an_absolute_path_to_a_file_or_a_name_on_path_counts_but_not_a_file_in_the_current_folder(self):
        with mock.patch.object(programs.shutil, "which", return_value=None):
            self.assertTrue(programs.available(__file__))
            self.assertFalse(programs.available(Path(__file__).name))
            self.assertFalse(programs.available(str(Path(__file__).parent)))
        with mock.patch.object(programs.shutil, "which", return_value="/usr/bin/alass-cli"):
            self.assertTrue(programs.available("alass-cli"))

    @unittest.skipUnless(os.name == "nt", "PATHEXT is a Windows notion")
    def test_an_absolute_path_without_its_exe_extension_counts_on_windows(self):
        (self.tmp / "alass-cli.exe").write_bytes(b"")
        self.assertTrue(programs.available(str(self.tmp / "alass-cli")))
        self.assertFalse(programs.available(str(self.tmp / "other")))

    def test_the_identity_of_a_tool_changes_with_its_file(self):
        exe = self.tmp / "tool.bin"
        exe.write_bytes(b"1")
        first = programs.identity(str(exe))
        self.assertIn(os.path.realpath(exe), first)
        exe.write_bytes(b"version two")
        self.assertNotEqual(programs.identity(str(exe)), first)
        self.assertEqual(programs.identity(str(self.tmp / "missing")), f"{self.tmp / 'missing'}: missing")
