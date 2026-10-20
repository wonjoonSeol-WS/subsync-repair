"""Tests for subsync_repair/formats/srt.py."""
import unittest

from subsync_repair.formats import srt
from support import TempFolder


class Parse(unittest.TestCase):
    def test_a_cue_keeps_its_text_as_written(self):
        text = "1\n00:00:01,000 --> 00:00:02,500\n<i>Hello</i>{\\an8} there\\Nfriend\n"
        self.assertEqual(srt.parse_srt(text), [srt.Cue(1.0, 2.5, "<i>Hello</i>{\\an8} there\\Nfriend")])

    def test_a_cue_split_by_a_blank_line_is_joined_back(self):
        text = "1\n00:00:01,000 --> 00:00:02,000\nfirst\n\nsecond\n\n2\n00:00:03,000 --> 00:00:04,000\nnext\n"
        self.assertEqual([c.text for c in srt.parse_srt(text)], ["first\nsecond", "next"])

    def test_a_timing_line_always_starts_a_new_cue(self):
        text = ("1\n00:00:01,000 --> 00:00:02,000\nfirst\n2\n00:00:03,000 --> 00:00:04,000\nsecond\n"
                "00:00:05,000 --> 00:00:06,000\nthird\n")
        self.assertEqual([(c.start, c.text) for c in srt.parse_srt(text)],
                         [(1.0, "first"), (3.0, "second"), (5.0, "third")])

    def test_a_cue_with_a_negative_time_is_left_out_with_its_continuation(self):
        text = ("1\n-00:00:01,000 --> 00:00:02,000\nbefore zero\n\nstill before\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\nafter\n")
        self.assertEqual([(c.start, c.text) for c in srt.parse_srt(text)], [(3.0, "after")])

    def test_times_with_any_fraction_length_or_a_dot(self):
        text = "1\n00:00:01,5 --> 00:00:02,25\nx\n\n2\n01:02:03.040 --> 01:02:04\ny\n"
        self.assertEqual([(c.start, c.end) for c in srt.parse_srt(text)], [(1.5, 2.25), (3723.04, 3724.0)])

    def test_windows_and_old_mac_line_endings_and_whitespace_only_separators(self):
        for text in ("1\r\n00:00:01,000 --> 00:00:02,000\r\nhi\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nyo\r\n",
                     "1\r00:00:01,000 --> 00:00:02,000\rhi\r\r2\r00:00:03,000 --> 00:00:04,000\ryo\r",
                     "1\n00:00:01,000 --> 00:00:02,000\nhi\n  \n2\n00:00:03,000 --> 00:00:04,000\nyo\n"):
            self.assertEqual([c.text for c in srt.parse_srt(text)], ["hi", "yo"], repr(text))


class Read(TempFolder):
    def test_read_srt_tolerates_a_byte_order_mark_and_bad_bytes(self):
        p = self.tmp / "x.srt"
        p.write_bytes(b"\xef\xbb\xbf1\n00:00:01,000 --> 00:00:02,000\nhi \xff\n")
        self.assertEqual(srt.read_srt(p)[0].start, 1.0)


class Write(unittest.TestCase):
    def test_format_time_rounds_to_milliseconds_and_clamps_below_zero(self):
        self.assertEqual([srt.format_time(t) for t in (0, 3723.0406, 59.9996, -2)],
                         ["00:00:00,000", "01:02:03,041", "00:01:00,000", "00:00:00,000"])

    def test_cues_without_text_are_left_out_and_the_rest_renumbered(self):
        cues = [srt.Cue(1.0, 2.0, "a"), srt.Cue(3.0, 4.0, " \n "), srt.Cue(5.0, 6.0, "b")]
        self.assertEqual(srt.format_with_text(cues), "1\n00:00:01,000 --> 00:00:02,000\na\n\n"
                                                      "2\n00:00:05,000 --> 00:00:06,000\nb\n\n")

    def test_normalize_renumbers_and_drops_cues_without_text(self):
        text = "7\n00:00:01,000 --> 00:00:02,000\nfirst\n\nsecond\n\n8\n00:00:03,000 --> 00:00:04,000\n\n\n9\n" \
               "00:00:05,000 --> 00:00:06,000\nlast\n"
        self.assertEqual(srt.normalize_srt(text), "1\n00:00:01,000 --> 00:00:02,000\nfirst\nsecond\n\n"
                                                  "2\n00:00:05,000 --> 00:00:06,000\nlast\n\n")

    def test_last_end_is_the_latest_end_not_the_last_cues(self):
        cues = [srt.Cue(0, 50, "a"), srt.Cue(10, 20, "b")]
        self.assertEqual(srt.last_end(cues), 50)
        self.assertIsNone(srt.last_end([]))


class Spoken(unittest.TestCase):
    def test_markup_is_removed_and_ass_line_breaks_become_real_ones(self):
        self.assertEqual(srt.spoken("<i>Hello</i>{\\an8} there\\Nfriend"), "Hello there\nfriend")


if __name__ == "__main__":
    unittest.main()
