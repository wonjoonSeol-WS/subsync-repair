"""The repair against the real alass-cli, for the correction shapes that must be fixed. alass-cli is taken from
PATH or from the SUBSYNC_REPAIR_ALASS environment variable; without it these tests skip."""
import os
import random
import shutil
import unittest

from subsync_repair.engine.repairer import Repairer
from subsync_repair.engine.verdicts import Status
from subsync_repair.external.alass import Alass
from subsync_repair.formats import srt
from support import FakeFfsubsync, FakeMedia, LibraryCase

ALASS = os.environ.get("SUBSYNC_REPAIR_ALASS") or shutil.which("alass-cli")


def english_track(count=240, opening_after=None, opening=90.0):
    """Cues at irregular times, as speech is; with an opening of `opening` seconds after cue `opening_after`."""
    rng = random.Random(7)
    cues, t = [], 5.0
    for i in range(count):
        length = rng.uniform(0.8, 2.6)
        cues.append(srt.Cue(round(t, 3), round(t + length, 3), f"English line {i}"))
        t += length + rng.uniform(0.3, 6.0) + (opening if i == opening_after else 0.0)
    return cues


@unittest.skipUnless(ALASS, "alass-cli is not installed (or set SUBSYNC_REPAIR_ALASS to it)")
class RealAlass(LibraryCase):
    def repair_against(self, reference, subtitle):
        self.sub.write_text(srt.format_srt(subtitle), encoding="utf-8")
        repairer = Repairer(self.settings, FakeMedia({"en": [reference]}), Alass(ALASS), FakeFfsubsync())
        return repairer.repair(self.sub, self.backups)

    @staticmethod
    def moved(cues, shift_of):
        return [srt.Cue(c.start + shift_of(i), c.end + shift_of(i), f"한국어 대사 {i}") for i, c in enumerate(cues)]

    def assert_starts(self, expected):
        found = [c.start for c in srt.read_srt(self.sub)]
        self.assertEqual(len(found), len(expected))
        self.assertLess(max(abs(a - b) for a, b in zip(found, expected)), 0.1)

    def test_a_subtitle_late_by_the_same_large_amount_throughout_is_fixed(self):
        reference = english_track()
        subtitle = self.moved([c for c in reference if c.start >= 45.0], lambda i: -45.0)
        verdict = self.repair_against(reference, subtitle)
        self.assertEqual(verdict.status, Status.FIXED, verdict.note)
        self.assert_starts([c.start + 45.0 for c in subtitle])

    def test_an_opening_missing_after_the_first_part_is_fixed(self):
        reference = english_track(opening_after=11)
        subtitle = self.moved(reference, lambda i: 0.0 if i <= 11 else -90.0)
        verdict = self.repair_against(reference, subtitle)
        self.assertEqual(verdict.status, Status.FIXED, verdict.note)
        self.assert_starts([c.start for c in reference])


if __name__ == "__main__":
    unittest.main()
