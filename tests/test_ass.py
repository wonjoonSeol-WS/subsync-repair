"""Tests for subsync_repair/formats/ass.py: reading ASS/SSA dialogue and shifting events."""
import unittest

from subsync_repair.formats import ass
from subsync_repair.formats.decoding import UndecodableText
from support import UNDECODABLE

HEADER = ("[Script Info]\nTitle: x\n\n[Events]\n"
          "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def dialogue(start, end, text="안녕하세요", kind="Dialogue", style="Default"):
    return f"{kind}: 0,{start},{end},{style},,0,0,0,,{text}\n"


class ShiftFor(unittest.TestCase):
    PIECES = [(10.0, 2.0), (100.0, -5.0), (200.0, 1.0)]

    def test_an_event_takes_the_shift_of_the_last_piece_that_started_before_it(self):
        self.assertEqual([ass.shift_for(t, self.PIECES) for t in (10, 99.9, 100, 150, 200, 999)],
                         [2.0, 2.0, -5.0, -5.0, 1.0, 1.0])

    def test_before_the_first_piece_the_first_shift_applies_and_no_pieces_means_no_shift(self):
        self.assertEqual(ass.shift_for(5.0, self.PIECES), 2.0)
        self.assertEqual(ass.shift_for(5, []), 0.0)


class Times(unittest.TestCase):
    def test_times_round_trip(self):
        for text in ("0:00:00.00", "0:01:05.50", "1:02:03.04", "10:00:00.99"):
            self.assertEqual(ass.ass_time(ass.ass_seconds(text)), text)

    def test_one_to_three_fraction_digits_are_read_and_anything_else_is_not(self):
        self.assertEqual([ass.ass_seconds(t) for t in ("0:00:01.5", "0:00:01.25", "0:00:01.125")], [1.5, 1.25, 1.125])
        for bad in ("0:00:01", "0:00:01.1234", "0:0:01.00", "not a time"):
            self.assertIsNone(ass.ass_seconds(bad), bad)


class Shift(unittest.TestCase):
    def test_only_dialogue_events_move_and_comments_and_headers_stay(self):
        text = HEADER + dialogue("0:00:10.00", "0:00:12.00") + dialogue("0:00:20.00", "0:00:21.00", kind="Comment")
        result = ass.shift_text(text, [(0.0, 5.0)])
        self.assertEqual((result.events, result.timed, result.moved, result.before_zero), (1, 1, 1, 0))
        self.assertIn("Dialogue: 0,0:00:15.00,0:00:17.00,", result.text)
        self.assertIn("Comment: 0,0:00:20.00,0:00:21.00,", result.text)
        self.assertTrue(result.text.startswith(HEADER))

    def test_commas_in_the_text_survive_the_field_split(self):
        result = ass.shift_text(HEADER + dialogue("0:00:10.00", "0:00:12.00", "a, b, c,,d"), [(0.0, 1.0)])
        self.assertIn(",,a, b, c,,d\n", result.text)

    def test_the_format_line_says_where_the_times_are(self):
        text = ("[Events]\nFormat: Start, End, Layer, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: 0:00:10.00,0:00:12.00,0,Default,,0,0,0,,x\n")
        self.assertIn("Dialogue: 0:00:15.00,0:00:17.00,0,Default", ass.shift_text(text, [(0.0, 5.0)]).text)

    def test_an_ssa_file_with_marked_instead_of_layer_shifts_the_same_way(self):
        text = ("[Events]\nFormat: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                "Dialogue: Marked=0,0:00:10.00,0:00:12.00,Default,,0000,0000,0000,,x\n")
        self.assertIn("Marked=0,0:00:12.00,0:00:14.00", ass.shift_text(text, [(0.0, 2.0)]).text)

    def test_a_shift_up_to_the_epsilon_is_not_counted_as_moved(self):
        event = HEADER + dialogue("0:00:10.00", "0:00:12.00")
        self.assertEqual(ass.shift_text(event, [(0.0, ass.MOVED_EPSILON)]).moved, 0)
        self.assertEqual(ass.shift_text(event, [(0.0, 0.25)]).moved, 1)

    def test_pieces_apply_by_the_events_own_start(self):
        text = HEADER + dialogue("0:00:10.00", "0:00:11.00") + dialogue("0:03:00.00", "0:03:01.00")
        result = ass.shift_text(text, [(0.0, 2.0), (100.0, -7.0)])
        self.assertIn("0:00:12.00,0:00:13.00", result.text)
        self.assertIn("0:02:53.00,0:02:54.00", result.text)

    def test_events_that_would_move_before_zero_are_counted_and_left_as_they_were(self):
        text = HEADER + dialogue("0:00:02.00", "0:00:03.00") + dialogue("0:00:10.00", "0:00:11.00")
        result = ass.shift_text(text, [(0.0, -5.0)])
        self.assertEqual(result.before_zero, 1)
        self.assertIn("Dialogue: 0,0:00:02.00,0:00:03.00", result.text)
        self.assertEqual(ass.shift_text(text, [(0.0, -2.0)]).before_zero, 0)

    def test_malformed_events_are_left_alone_and_counted(self):
        text = "Dialogue: only,three,fields\nDialogue: 0,not a time,0:00:01.00,Default,,0,0,0,,x\n"
        result = ass.shift_text(text, [(0.0, 5.0)])
        self.assertEqual((result.text, result.events, result.timed, result.moved), (text, 2, 0, 0))

    def test_windows_line_endings_are_kept(self):
        result = ass.shift_text((HEADER + dialogue("0:00:10.00", "0:00:12.00")).replace("\n", "\r\n"), [(0.0, 5.0)])
        self.assertEqual(result.text.count("\r\n"), (HEADER + "x\n").count("\n"))
        self.assertIn("0:00:15.00,0:00:17.00,Default,,0,0,0,,안녕하세요\r\n", result.text)


class ShiftProblem(unittest.TestCase):
    def test_every_event_must_be_understood_stay_after_zero_and_some_must_move(self):
        self.assertIsNone(ass.shift_problem(ass.ShiftResult("", 3, 3, 1, 0)))
        self.assertIn("1 of its 3 events have times", ass.shift_problem(ass.ShiftResult("", 3, 2, 2, 0)))
        self.assertIn("1 of its 3 events would start before 0:00", ass.shift_problem(ass.ShiftResult("", 3, 3, 2, 1)))
        self.assertIn("none of its events would move", ass.shift_problem(ass.ShiftResult("", 3, 3, 0, 0)))


class Dialogue(unittest.TestCase):
    def test_dialogue_text_is_just_the_spoken_part_including_commas(self):
        text = HEADER + dialogue("0:00:01.00", "0:00:02.00", "안녕, 잘 지내세요") + dialogue("0:00:05.00", "0:00:06.00",
                                                                                        "주석", kind="Comment")
        self.assertEqual(ass.dialogue_text(text), "안녕, 잘 지내세요")
        self.assertEqual(ass.dialogue_text("no events here"), "")

    def test_dialogue_cues_leave_out_signs_karaoke_drawings_and_song_styles(self):
        text = HEADER + "".join((
            dialogue("0:00:30.00", "0:00:31.00", "{\\i1}둘째{\\i0}\\N줄"),
            dialogue("0:00:10.00", "0:00:11.00", "첫째\\h말"),
            dialogue("0:00:12.00", "0:00:13.00", "{\\pos(10,20)}간판"),
            dialogue("0:00:14.00", "0:00:15.00", "{\\move(1,2,3,4)}움직임"),
            dialogue("0:00:16.00", "0:00:17.00", "{\\k20}가{\\k30}라"),
            dialogue("0:00:18.00", "0:00:19.00", "{\\p1}m 0 0 l 1 1{\\p0}"),
            dialogue("0:00:20.00", "0:00:21.00", "오프닝", style="OP_Romaji"),
            dialogue("0:00:22.00", "0:00:23.00", "제목", style="Signs"),
            dialogue("0:00:24.00", "0:00:25.00", "대사", style="Subtitle"),
            dialogue("0:00:26.00", "0:00:27.00", "", style="Default"),
            dialogue("0:00:28.00", "0:00:29.00", "주석", kind="Comment")))
        self.assertEqual([(c.start, c.text) for c in ass.dialogue_cues(text)],
                         [(10.0, "첫째 말"), (24.0, "대사"), (30.0, "둘째\n줄")])

    def test_shares_timing_needs_most_srt_cues_to_start_with_an_event(self):
        text = HEADER + "".join(dialogue(f"0:00:{s:02d}.00", f"0:00:{s + 1:02d}.00") for s in range(0, 50, 5))
        self.assertTrue(ass.shares_timing(text, [0.05, 5.0, 10.08, 15.0, 20.0]))
        self.assertTrue(ass.shares_timing(text, [0.0, 5.0, 10.0, 15.0, 22.5]))          # four of five
        self.assertFalse(ass.shares_timing(text, [0.0, 5.0, 12.5, 17.5, 22.5]))
        self.assertFalse(ass.shares_timing(text, [s + 3.0 for s in range(0, 50, 5)]))
        self.assertFalse(ass.shares_timing(text, []))

    def test_an_event_exactly_the_same_timing_apart_still_starts_together(self):
        text = HEADER + "".join(dialogue(t, "0:00:59.00") for t in ("0:00:00.10", "0:00:05.00", "0:00:10.00",
                                                                     "0:00:15.00", "0:00:22.50"))
        self.assertTrue(ass.shares_timing(text, [0.0, 5.0, 10.0, 15.0, 20.0]))
        self.assertFalse(ass.shares_timing(text, [-0.01, 5.0, 10.0, 15.0, 20.0]))


class Bytes(unittest.TestCase):
    def test_cp949_and_utf16_files_are_read_and_a_file_that_is_not_text_is_refused(self):
        text = HEADER + dialogue("0:00:10.00", "0:00:12.00", "안녕하세요 잘 지내세요")
        for raw, encoding in ((text.encode("cp949"), "cp949"), (text.encode("utf-16"), "utf-16")):
            decoded = ass.read(raw, "ko")
            self.assertIn("안녕하세요 잘 지내세요", decoded.text)
            self.assertEqual(decoded.encoding, encoding)
        with self.assertRaises(UndecodableText):
            ass.read(UNDECODABLE, "ko")

    def test_the_language_decides_which_legacy_encoding_is_tried(self):
        text = HEADER + dialogue("0:00:10.00", "0:00:12.00", "こんにちは、元気ですか。私は学生です")
        self.assertIn("こんにちは", ass.read(text.encode("cp932"), "ja").text)


if __name__ == "__main__":
    unittest.main()
