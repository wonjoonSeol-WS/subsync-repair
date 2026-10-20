"""Tests for subsync_repair/storage/datadir.py: making the data folder, sharing it on Windows, and refusing one that
another account planted."""
import os
import subprocess
import unittest
from unittest import mock

from subsync_repair.external import process
from subsync_repair.storage import datadir
from subsync_repair.storage.datadir import DataDirError
from support import TempFolder, completed

WINDOWS_ONLY = unittest.skipUnless(os.name == "nt", "the shared data folder is a Windows arrangement")


class Prepare(TempFolder):
    def test_a_missing_folder_is_made_with_its_parents(self):
        folder = self.tmp / "a" / "b" / "data"
        with mock.patch.object(datadir, "share_with_users", return_value=True):
            self.assertEqual(datadir.prepare(folder), folder)
        self.assertTrue(folder.is_dir())

    @WINDOWS_ONLY
    def test_a_new_folder_lets_every_local_user_modify_what_it_holds(self):
        folder = self.tmp / "data"
        datadir.prepare(folder)
        acl = subprocess.run(["icacls", str(folder)], capture_output=True, text=True, encoding="mbcs").stdout
        self.assertIn("(OI)(CI)(M)", acl)

    @WINDOWS_ONLY
    def test_a_failing_grant_is_a_warning_with_what_icacls_said(self):
        failed = completed(stderr="Access is denied.", code=5)
        with mock.patch.object(process, "run", return_value=failed), \
                self.assertLogs("subsync_repair.storage.datadir", "WARNING") as logs:
            self.assertFalse(datadir.share_with_users(self.tmp))
        self.assertIn("Access is denied.", logs.output[0])

    @WINDOWS_ONLY
    def test_a_folder_of_this_account_or_an_administrator_is_used(self):
        self.assertEqual(datadir.owner_sid(self.tmp), datadir.current_sid())
        self.assertEqual(datadir.prepare(self.tmp), self.tmp)
        with mock.patch.object(datadir, "owner_sid", return_value="S-1-5-32-544"):
            self.assertEqual(datadir.prepare(self.tmp), self.tmp)

    @WINDOWS_ONLY
    def test_a_folder_another_account_owns_is_refused(self):
        stranger = "S-1-5-21-1-2-3-4242"
        with mock.patch.object(datadir, "owner_sid", return_value=stranger):
            with self.assertRaisesRegex(DataDirError, f"another account \\({stranger}\\)"):
                datadir.prepare(self.tmp)

    @WINDOWS_ONLY
    def test_an_owner_that_cannot_be_read_is_a_warning(self):
        with mock.patch.object(datadir, "owner_sid", side_effect=OSError(5, "denied")), \
                self.assertLogs("subsync_repair.storage.datadir", "WARNING"):
            self.assertEqual(datadir.prepare(self.tmp), self.tmp)


if __name__ == "__main__":
    unittest.main()
