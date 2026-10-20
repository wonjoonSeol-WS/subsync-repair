"""Tests for subsync_repair/engine/verdicts.py."""
import unittest

from subsync_repair.engine.verdicts import NOTE_MAX, Status, clip, error, file_error, os_reason, refuse, skip


class Verdicts(unittest.TestCase):
    def test_a_note_up_to_the_limit_is_kept_and_a_longer_one_is_clipped(self):
        self.assertEqual(clip("x" * NOTE_MAX), "x" * NOTE_MAX)
        self.assertEqual(clip("x" * (NOTE_MAX + 1)), "x" * (NOTE_MAX - 3) + "...")

    def test_refusals_are_cacheable_unless_they_are_errors(self):
        self.assertTrue(skip("x").verdict.cacheable)
        self.assertFalse(error("x").verdict.cacheable)
        self.assertTrue(error("x", cacheable=True).verdict.cacheable)
        self.assertFalse(refuse(Status.MISMATCH, "x", cacheable=False).verdict.cacheable)

    def test_a_file_error_names_the_reason_keeps_the_exception_for_the_log_and_is_tried_again(self):
        e = PermissionError(13, "Access is denied", "/media/ep.ko.srt")
        verdict = file_error(e)
        self.assertEqual((verdict.status, verdict.note, verdict.detail), (Status.ERROR, "file error (Access is denied)",
                                                                          repr(e)))
        self.assertFalse(verdict.cacheable)

    def test_an_os_error_without_a_reason_is_named_by_its_kind(self):
        self.assertEqual(os_reason(OSError(2, "No such file")), "No such file")
        self.assertEqual(os_reason(PermissionError()), "PermissionError")


if __name__ == "__main__":
    unittest.main()
