"""Tests for subsync_repair.repair and writers.py: measuring, copying and repairing one subtitle. alass is a fake that
moves cue start times, so every decision can be exercised; the .smi conversion uses the real ffmpeg and is skipped
without it."""
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from subsync_repair.engine import policy, references, writers
from subsync_repair.engine.repairer import Repairer, verdict_key
from subsync_repair.engine.verdicts import Status
from subsync_repair.external.audio_check import ReferenceCheck
from subsync_repair.formats import ass, srt
from subsync_repair.formats.decoding import UTF8_BOM
from subsync_repair.storage import fileio
from support import (ASS_HEADER, CUES, ENGLISH, GERMAN, HAVE_FFMPEG, JAPANESE, KOREAN, REFERENCE_CUES, STARTS,
                     UNDECODABLE, FakeAligner, FakeFfsubsync, LibraryCase, another_episode_shift, ass_events,
                     hard_links_work, make_file_link, make_settings, srt_of, starts_with_gap, track)



class Measuring(LibraryCase):
    def test_a_late_subtitle_is_reported_with_its_shifts_and_nothing_is_written(self):
        before = self.sub.read_bytes()
        verdict = self.repairer.measure(self.sub)
        self.assertEqual(verdict.status, Status.WOULD_FIX)
        self.assertIn("+5.0s", verdict.note)
        self.assertIn("[ref: embedded en]", verdict.note)
        self.assertEqual((verdict.shifts, verdict.reference), (((0.0, 5.0),), "embedded en"))
        self.assertEqual(self.sub.read_bytes(), before)
        self.assertEqual(self.backed_up(), [])

    def test_the_threshold_decides_what_is_out_of_sync(self):
        cases = ((0.2, Status.OK), (policy.THRESHOLD, Status.OK), (policy.THRESHOLD + 0.05, Status.WOULD_FIX))
        for shift, expected in cases:
            self.aligner.shift = shift
            self.assertEqual(self.repairer.measure(self.sub).status, expected, shift)

    def test_one_cue_moved_beyond_the_threshold_inside_a_piece_is_out_of_sync(self):
        self.aligner.shift = lambda s: 0.9 if s == STARTS[-1] else 0.6
        verdict = self.repairer.measure(self.sub)
        self.assertEqual((verdict.status, verdict.shifts), (Status.WOULD_FIX, ((0.0, 0.6),)))

    def test_a_file_in_sync_is_ok_and_its_shifts_are_still_reported(self):
        self.aligner.shift = 0.1
        verdict = self.repairer.measure(self.sub)
        self.assertEqual((verdict.status, verdict.note), (Status.OK, "in sync"))
        self.assertEqual(verdict.as_dict(self.sub)["shifts"], [[0.0, 0.1]])

    def test_piecewise_drift_lists_every_piece(self):
        self.aligner.shift = lambda s: 2.0 if s < 100 else -7.0
        verdict = self.repairer.measure(self.sub)
        self.assertEqual(verdict.shifts, ((0.0, 2.0), (100.0, -7.0)))
        self.assertIn("0:00 +2.0s, 1:40 -7.0s", verdict.note)

    def test_the_first_piece_starts_at_the_first_cue(self):
        self.sub.write_text(srt_of([95.0 + s for s in STARTS], KOREAN), encoding="utf-8")
        verdict = self.repairer.measure(self.sub)
        self.assertEqual(verdict.shifts, ((95.0, 5.0),))
        self.assertIn("shifts 1:35 +5.0s", verdict.note)

    def test_the_working_srt_and_the_reference_reach_the_aligner(self):
        self.repairer.measure(self.sub)
        reference, work = self.aligner.calls[0]
        self.assertEqual([c.text for c in work], KOREAN)
        self.assertEqual(len(reference), REFERENCE_CUES)


class Refusals(LibraryCase):
    def expect(self, status, fragment, verdict=None):
        before = self.sub.read_bytes()
        verdict = verdict or self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, status, verdict.note)
        self.assertIn(fragment, verdict.note)
        self.assertEqual(self.sub.read_bytes(), before, "a refused file must not change")
        self.assertEqual(self.backed_up(), [])
        return verdict

    def test_no_reference_is_a_quiet_skip(self):
        self.media.tracks = {}
        verdict = self.expect(Status.SKIP, "no reference")
        self.assertTrue(verdict.quiet)
        self.assertEqual(self.aligner.calls, [])

    def test_a_subtitle_that_does_not_decode(self):
        self.sub.write_bytes(UNDECODABLE)
        self.expect(Status.SKIP, "cannot decode the .srt file (text does not read as Korean)")

    def test_alass_failing_is_an_error_whose_tool_output_stays_in_the_detail(self):
        self.aligner.fail = ("alass failed", "thread 'main' panicked at C:\\private\\src")
        verdict = self.expect(Status.ERROR, "alass failed")
        self.assertNotIn("private", verdict.note)
        self.assertIn("private", verdict.detail)
        self.assertFalse(verdict.cacheable)

    def test_alass_changing_the_text(self):
        self.aligner.texts = [f"다른 글 {i}" for i in range(CUES)]
        self.expect(Status.SKIP, "changed the subtitle text")

    def test_whitespace_only_changes_by_alass_are_fine(self):
        self.aligner.texts = [f"안녕하세요  잘 지내세요 {i} " for i in range(CUES)]
        self.assertEqual(self.repairer.measure(self.sub).status, Status.WOULD_FIX)

    def test_a_dropped_cue_counts_as_a_text_change(self):
        self.aligner.starts_fn, self.aligner.texts = (lambda starts: starts[:-1]), KOREAN[:-1]
        self.expect(Status.SKIP, "changed the subtitle text")

    def test_a_file_with_too_few_cues_is_refused_before_anything_expensive_runs(self):
        self.sub.write_text(srt_of(STARTS[:policy.MIN_CUES - 1], KOREAN), encoding="utf-8")
        self.expect(Status.SKIP, "too few cues")
        self.assertEqual((self.aligner.calls, self.media.asked), ([], []))
        self.sub.write_text(srt_of(STARTS[:policy.MIN_CUES], KOREAN), encoding="utf-8")
        self.assertEqual(self.repairer.measure(self.sub).status, Status.WOULD_FIX)

    def test_a_correction_that_is_too_large(self):
        self.aligner.shift = lambda s: 0.0 if s < 100 else policy.MAX_SHIFT + 20      # most of it moves together
        self.expect(Status.SKIP, "implausible")

    def test_many_pieces_with_a_large_shift_are_a_wrong_reference(self):
        self.aligner.shift = lambda s: 10.0 + 10.0 * (int(s // 40) % 2)
        self.expect(Status.SKIP, "implausible")

    def test_a_start_that_keeps_changing(self):
        ramp = {0: 0.9, 1: 4.0, 2: 6.3}
        self.aligner.shift = lambda s: ramp.get(int(s // 10), 9.1)
        self.expect(Status.SKIP, "keeps changing in the first 30 s")

    def test_cues_stacked_onto_one_start_time(self):
        self.aligner.starts_fn = lambda st: [s + 5.0 for s in st[:5]] + [55.0, 55.0, 55.0] + [s + 5.0 for s in st[8:]]
        self.expect(Status.SKIP, "stacked")

    def test_a_result_that_runs_past_the_end_of_the_video(self):
        self.media.video_duration = 60.0
        self.expect(Status.SKIP, "run past the end")

    def test_a_refusal_after_the_alignment_still_reports_its_shifts(self):
        self.aligner.shift = policy.MAX_SHIFT + 20
        verdict = self.repairer.measure(self.sub)
        self.assertEqual((verdict.status, verdict.shifts[0][1], verdict.reference),
                         (Status.SKIP, policy.MAX_SHIFT + 20, "embedded en"))

    def test_forced_signs_and_the_servers_own_copies_are_not_repaired(self):
        for name in ("Show S01E01.ko.forced.srt", "Show S01E01.ko.signs.srt", "Show S01E01.ko.synced.srt", "notes.txt"):
            path = self.write(name, srt_of(STARTS, KOREAN))
            verdict = self.repairer.repair(path, self.backups)
            self.assertEqual(verdict.status, Status.SKIP, name)
            self.assertIn("not a subtitle this tool repairs", verdict.note)
        self.assertEqual(self.aligner.calls, [])

    def test_a_missing_file_is_an_error_result_not_a_crash(self):
        verdict = self.repairer.measure(self.dir / "Show S01E01.ko.hi.srt")
        self.assertEqual(verdict.status, Status.ERROR)
        self.assertIn("file error", verdict.note)

    def test_an_unexpected_failure_is_an_error_result_not_a_crash(self):
        with mock.patch.object(Repairer, "align", side_effect=RuntimeError("boom at C:\\private")), \
                self.assertLogs("subsync_repair.engine.repairer", "ERROR"):
            verdict = self.repairer.measure(self.sub)
        self.assertEqual((verdict.status, verdict.note), (Status.ERROR, "unexpected error"))
        self.assertIn("boom", verdict.detail)


class Mismatch(LibraryCase):
    """Fixtures shaped like real alass measurements against a video's own English track."""

    def use(self, starts, shift_of):
        self.sub.write_text(srt_of(starts, [f"안녕하세요 {i}" for i in range(len(starts))]), encoding="utf-8")
        self.aligner.starts_fn = lambda found: [s + shift_of(i, s) for i, s in enumerate(found)]

    def test_a_subtitle_of_another_episode_is_a_mismatch_and_is_left_alone(self):
        starts = [200.0 + 10 * i for i in range(60)]
        self.use(starts, lambda i, s: another_episode_shift(i, s, starts))
        before = self.sub.read_bytes()
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.MISMATCH)
        self.assertIn("does not match this video (different episode or cut?)", verdict.note)
        self.assertTrue(verdict.cacheable)
        self.assertEqual(self.sub.read_bytes(), before)
        self.assertEqual(self.backed_up(), [])

    def fixed(self):
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.FIXED, verdict.note)
        return verdict

    def test_a_block_in_the_middle_that_drifted_is_fixed(self):
        self.use(starts_with_gap(264, after=170, gap=30.0), lambda i, s: 18.0 if 136 <= i <= 170 else 0.1)
        self.fixed()

    def test_a_cold_open_that_drifted_is_fixed(self):
        self.use(starts_with_gap(306, after=25, gap=15.0), lambda i, s: 10.17 if i < 26 else 0.0)
        self.assertIn("0:00 +10.2s", self.fixed().note)

    def test_a_subtitle_late_by_the_same_large_amount_throughout_is_fixed(self):
        starts = starts_with_gap(264, after=0, gap=0.0)
        self.use(starts, lambda i, s: 45.0)
        self.fixed()
        self.assertEqual(self.starts_of(self.sub), [s + 45.0 for s in starts])

    def test_an_opening_missing_after_the_first_part_is_fixed(self):
        self.use(starts_with_gap(264, after=5, gap=3.0), lambda i, s: 0.0 if i < 6 else 90.0)
        self.assertIn("+90.0s", self.fixed().note)


class AnyLanguage(LibraryCase):
    """The language of a subtitle comes from its own name; its reference is never in that language."""

    SUBTITLE = "Show S01E01.ja.srt"
    TEXTS = JAPANESE

    def test_a_japanese_subtitle_is_repaired_against_the_english_track(self):
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual((verdict.status, verdict.reference), (Status.FIXED, "embedded en"))
        self.assertEqual(self.starts_of(self.sub), [s + 5.0 for s in STARTS])
        self.assertEqual(srt.read_srt(self.sub)[0].text, JAPANESE[0])

    def test_a_track_in_the_subtitles_own_language_is_never_its_reference(self):
        self.media.tracks = {"ja": [track(90, JAPANESE)], "en": [track()]}
        settings = make_settings(self.root, self.data, "ko", embedded_reference_languages=["ja", "en"])
        verdict = self.repairer_for(settings).measure(self.sub)
        self.assertEqual(verdict.reference, "embedded en")
        self.assertNotIn(("Show S01E01.mkv", "ja"), self.media.asked)

    def test_when_the_only_candidate_reference_is_its_own_language_it_is_refused(self):
        self.media.tracks = {"ja": [track(90, JAPANESE)]}
        self.write("Show S01E01.ja.hi.srt", srt_of(STARTS, JAPANESE))
        settings = make_settings(self.root, self.data, "ko", embedded_reference_languages=["ja"],
                                 reference_languages=["ja"])
        verdict = self.repairer_for(settings).repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertIn("no reference", verdict.note)
        self.assertEqual((self.aligner.calls, self.ffsubsync.calls), ([], []))


class GermanTarget(LibraryCase):
    """Nothing is tied to one language: a German setup behaves the same way."""

    TARGET = "de"
    SUBTITLE = "Show S01E01.de.srt"
    TEXTS = GERMAN

    def test_a_german_subtitle_is_repaired(self):
        self.assertEqual(self.repairer.repair(self.sub, self.backups).status, Status.FIXED)
        self.assertEqual(srt.read_srt(self.sub)[0].text, GERMAN[0])

    def test_a_cp1252_german_subtitle_is_written_back_as_utf8_with_a_byte_order_mark(self):
        texts = [f"Schöne Grüße, wie geht's {i}" for i in range(CUES)]
        self.sub.write_bytes(srt_of(STARTS, texts).encode("cp1252"))
        self.assertEqual(self.repairer.repair(self.sub, self.backups).status, Status.FIXED)
        self.assertTrue(self.sub.read_bytes().startswith(UTF8_BOM))
        self.assertEqual(srt.read_srt(self.sub)[0].text, texts[0])


class Videos(LibraryCase):
    def test_the_video_is_the_one_whose_name_the_subtitle_extends(self):
        self.write("Show S01E02.mkv", b"")
        other = self.write("Show S01E02.ko.srt", srt_of(STARTS, KOREAN))
        self.repairer.measure(other)
        self.assertEqual(self.media.asked, [("Show S01E02.mkv", "en")])
        self.media.asked.clear()
        self.repairer.measure(self.sub)
        self.assertEqual(self.media.asked, [("Show S01E01.mkv", "en")])

    def test_a_longer_video_name_wins_over_its_prefix(self):
        self.write("Movie.avi", b"")
        self.write("Movie.en.mkv", b"")
        sub = self.write("Movie.en.ko.srt", srt_of(STARTS, KOREAN))
        verdict = self.repairer.measure(sub)
        self.assertEqual(self.media.asked, [("Movie.en.mkv", "en")])
        self.assertEqual(verdict.status, Status.WOULD_FIX)


class Siblings(LibraryCase):
    """No embedded track: a subtitle file next to the video in a reference language, checked by ffsubsync first."""

    def setUp(self):
        super().setUp()
        self.media.tracks = {}
        self.write("Show S01E01.ja.srt", srt_of(STARTS + [600.0], JAPANESE + ["こんにちは、最後です"]))

    def test_a_verified_sibling_is_used(self):
        verdict = self.repairer.measure(self.sub)
        self.assertEqual(verdict.status, Status.WOULD_FIX)
        self.assertIn("[ref: sibling ja]", verdict.note)
        self.assertEqual(self.ffsubsync.calls, [("Show S01E01.mkv", "reference.srt")])

    def test_an_unverified_sibling_is_refused_before_alass_runs(self):
        self.ffsubsync.result = self.ffsubsync.result._replace(ok=False, why="reference offset +9.00s")
        verdict = self.repairer.measure(self.sub)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertIn("reference not verified: reference offset +9.00s", verdict.note)
        self.assertTrue(verdict.cacheable)
        self.assertEqual(self.aligner.calls, [])

    def japanese_first(self):
        self.write("Show S01E01.en.srt", srt_of(STARTS, ENGLISH))
        settings = make_settings(self.root, self.data, "ko", reference_languages=["ja", "en"])
        return self.repairer_for(settings)

    def test_the_next_candidate_is_tried_when_the_first_does_not_verify(self):
        repairer = self.japanese_first()
        self.ffsubsync.decide = lambda text: ReferenceCheck(ok="Hello" in text, why="checked", lasting=True)
        verdict = repairer.measure(self.sub)
        self.assertIn("[ref: sibling en]", verdict.note)
        self.assertEqual(len(self.ffsubsync.calls), 2)

    def test_when_no_candidate_verifies_the_first_refusal_is_reported_and_kept_only_if_every_one_lasts(self):
        repairer = self.japanese_first()
        self.ffsubsync.decide = lambda text: ReferenceCheck(ok=False, why="en" if "Hello" in text else "ja",
                                                            lasting="Hello" not in text)
        verdict = repairer.measure(self.sub)
        self.assertEqual((verdict.status, verdict.cacheable), (Status.SKIP, False))
        self.assertIn("reference not verified: ja", verdict.note)

    def test_an_in_sync_verdict_is_only_reached_with_a_verified_sibling(self):
        self.aligner.shift = 0.0
        self.ffsubsync.result = self.ffsubsync.result._replace(ok=False, why="ffsubsync timed out", lasting=False)
        verdict = self.repairer.measure(self.sub)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertFalse(verdict.cacheable, "a failed check is tried again next time")

    def test_one_check_per_sibling_and_video_until_one_of_them_changes(self):
        other = self.write("Show S01E01.ko.hi.srt", srt_of(STARTS, KOREAN))
        self.repairer.measure(self.sub)
        self.repairer.measure(other)
        self.assertEqual(len(self.ffsubsync.calls), 1)
        self.write("Show S01E01.ja.srt", srt_of(STARTS + [610.0], JAPANESE + ["こんにちは、最後ですね"]))
        self.repairer.measure(self.sub)
        self.assertEqual(len(self.ffsubsync.calls), 2)

    def test_a_check_that_may_change_is_asked_again(self):
        self.ffsubsync.result = self.ffsubsync.result._replace(ok=False, why="timed out", lasting=False)
        self.repairer.measure(self.sub)
        self.repairer.measure(self.sub)
        self.assertEqual(len(self.ffsubsync.calls), 2)

    def test_a_reference_longer_than_its_video_is_from_another_cut(self):
        self.media.video_duration = 300.0
        verdict = self.repairer.measure(self.sub)
        self.assertIn("the reference runs 10 min but the video is 5 min", verdict.note)
        self.assertEqual(self.ffsubsync.calls, [])

    def test_a_reference_may_run_up_to_the_slack_past_its_video(self):
        last_end = 600.0 + 1.5
        self.media.video_duration = last_end - references.OTHER_CUT_SLACK
        self.assertIn("[ref: sibling ja]", self.repairer.measure(self.sub).note)
        self.media.video_duration -= 1.0
        self.assertIn("the reference runs", self.repairer.measure(self.sub).note)

    def test_why_ffsubsync_refused_a_reference_goes_to_the_detail(self):
        self.ffsubsync.result = ReferenceCheck(ok=False, why="ffsubsync failed", lasting=False, detail="Traceback")
        verdict = self.repairer.measure(self.sub)
        self.assertEqual((verdict.note, verdict.detail), ("reference not verified: ffsubsync failed", "Traceback"))

    def test_without_a_video_there_is_nothing_to_check_the_reference_against(self):
        self.video.unlink()
        self.assertIn("no video to check the sibling ja reference", self.repairer.measure(self.sub).note)

    def test_the_language_order_decides_and_forced_sign_and_synced_files_never_count(self):
        (self.dir / "Show S01E01.ja.srt").unlink()
        for name in ("Show S01E01.en.srt", "Show S01E01.ja.signs.srt", "Show S01E01.ja.forced.srt",
                     "Show S01E01.ja.synced.srt"):
            self.write(name, srt_of(STARTS, ENGLISH if ".en." in name else JAPANESE))
        settings = make_settings(self.root, self.data, "ko", reference_languages=["ja", "en"])
        repairer = self.repairer_for(settings)
        self.assertIn("[ref: sibling en]", repairer.measure(self.sub).note)
        self.write("Show S01E01.jpn.hi.srt", srt_of(STARTS, JAPANESE))
        self.assertIn("[ref: sibling ja]", repairer.measure(self.sub).note)

    def test_within_a_language_the_plain_file_comes_first_then_the_fullest(self):
        (self.dir / "Show S01E01.ja.srt").unlink()
        self.write("Show S01E01.ja.cc.srt", srt_of(STARTS, ["ちいさい"] * CUES))
        self.write("Show S01E01.ja.hi.srt", srt_of(STARTS, ["おおきいファイルです、ほんとうに"] * CUES))
        self.repairer.measure(self.sub)
        self.assertIn("おおきい", self.aligner.calls[-1][0][0].text)
        self.write("Show S01E01.ja.srt", srt_of(STARTS, ["ふつう"] * CUES))
        self.repairer.measure(self.sub)
        self.assertEqual(self.aligner.calls[-1][0][0].text, "ふつう")

    def test_only_srt_siblings_are_references(self):
        (self.dir / "Show S01E01.ja.srt").unlink()
        self.write("Show S01E01.ja.ass", ASS_HEADER + ass_events(STARTS, JAPANESE))
        self.assertIn("no reference", self.repairer.measure(self.sub).note)

    def test_another_cut_with_a_longer_name_is_not_a_reference(self):
        (self.dir / "Show S01E01.ja.srt").unlink()
        self.write("Show S01E01.Directors.Cut.ja.srt", srt_of(STARTS, JAPANESE))
        self.assertIn("no reference", self.repairer.measure(self.sub).note)

    def test_an_unreadable_reference(self):
        self.write("Show S01E01.ja.srt", UNDECODABLE)
        self.assertIn("reference subtitle cannot be read", self.repairer.measure(self.sub).note)

    def test_a_cp932_reference_with_blank_lines_inside_cues_is_cleaned(self):
        lines = "".join(f"{i}\n{srt.format_time(s)} --> {srt.format_time(s + 1)}\n{t}\n\n続き\n\n"
                        for i, (s, t) in enumerate(zip(STARTS, JAPANESE), 1))
        self.write("Show S01E01.ja.srt", lines.encode("cp932"))
        self.repairer.measure(self.sub)
        reference, _ = self.aligner.calls[0]
        self.assertEqual(reference[0].text, JAPANESE[0] + "\n続き")


class AssWithSrtTwin(LibraryCase):
    def test_a_repair_or_its_preview_leaves_an_ass_that_shares_its_timing_to_its_srt(self):
        ass_file = self.write("Show S01E01.ko.ass", ASS_HEADER + ass_events(STARTS, KOREAN))
        for verdict in (self.repairer.repair(ass_file, self.backups), self.repairer.measure(ass_file, preview=True)):
            self.assertEqual((verdict.status, verdict.quiet, verdict.cacheable), (Status.SKIP, True, True))
            self.assertIn("fixed together with its .srt twin Show S01E01.ko.srt", verdict.note)
        self.assertEqual(self.aligner.calls, [])

    def test_a_measurement_or_a_copy_of_such_an_ass_is_made_on_its_own(self):
        ass_file = self.write("Show S01E01.ko.ass", ASS_HEADER + ass_events(STARTS, KOREAN))
        self.assertEqual(self.repairer.measure(ass_file).status, Status.WOULD_FIX)
        dest = self.dir / "Show S01E01.ko.ass.synced.srt"
        self.assertEqual(self.repairer.copy(ass_file, dest).status, Status.FIXED)
        self.assertEqual(self.starts_of(dest), [s + 5.0 for s in STARTS])

    def test_only_the_ass_the_srt_pairs_with_is_left_to_it(self):
        paired = self.write("Show S01E01.ko.ass", ASS_HEADER + ass_events(STARTS, KOREAN))
        other = self.write("Show S01E01.ko.hi.ass", ASS_HEADER + ass_events(STARTS, KOREAN))
        verdict = self.repairer.repair(other, self.backups)
        self.assertEqual(verdict.status, Status.FIXED, verdict.note)
        self.assertIn(f"Dialogue: 0,{ass.ass_time(STARTS[1] + 5.0)},", other.read_text(encoding="utf-8"))
        self.assertEqual(self.repairer.repair(paired, self.backups).status, Status.SKIP)
        self.assertEqual(self.repairer.repair(self.sub, self.backups).status, Status.FIXED)
        self.assertIn(f"Dialogue: 0,{ass.ass_time(STARTS[1] + 5.0)},", paired.read_text(encoding="utf-8"))

    def test_an_ass_with_its_own_timing_is_repaired_on_its_own(self):
        ass_file = self.write("Show S01E01.ko.ass", ASS_HEADER + ass_events([s + 3.0 for s in STARTS], KOREAN))
        srt_before = self.sub.read_bytes()
        verdict = self.repairer.repair(ass_file, self.backups)
        self.assertEqual(verdict.status, Status.FIXED, verdict.note)
        self.assertIn("dialogue events shifted", verdict.note)
        self.assertIn(f"Dialogue: 0,{ass.ass_time(STARTS[1] + 8.0)},", ass_file.read_text(encoding="utf-8"))
        self.assertEqual(self.sub.read_bytes(), srt_before)


class InPlaceRepairs(LibraryCase):
    def test_an_srt_is_rewritten_after_a_backup(self):
        original = self.sub.read_bytes()
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.FIXED)
        self.assertEqual(self.starts_of(self.sub), [s + 5.0 for s in STARTS])
        self.assertEqual([p.read_bytes() for p in self.backed_up()], [original])
        self.assertTrue(self.backups.folder.name.startswith("repair-"))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["Show S01E01.ko.srt", "Show S01E01.mkv"])

    def test_every_cue_gets_the_time_alass_found_for_it(self):
        self.aligner.shift = lambda s: 5.0 + s / 1000          # varies inside one piece
        self.assertEqual(self.repairer.repair(self.sub, self.backups).shifts, ((0.0, 5.0),))
        self.assertEqual(self.starts_of(self.sub), [round(s + 5.0 + s / 1000, 3) for s in STARTS])

    def test_windows_line_endings_and_a_byte_order_mark_are_kept(self):
        self.sub.write_bytes(UTF8_BOM + srt_of(STARTS, KOREAN).replace("\n", "\r\n").encode("utf-8"))
        self.repairer.repair(self.sub, self.backups)
        data = self.sub.read_bytes()
        self.assertTrue(data.startswith(UTF8_BOM))
        self.assertEqual(data.count(b"\r\n"), data.count(b"\n"))

    def test_utf8_without_a_byte_order_mark_stays_without_one(self):
        self.repairer.repair(self.sub, self.backups)
        self.assertFalse(self.sub.read_bytes().startswith(UTF8_BOM))

    def test_a_cp949_srt_is_written_back_as_utf8_with_a_byte_order_mark(self):
        self.sub.write_bytes(srt_of(STARTS, KOREAN).encode("cp949"))
        self.repairer.repair(self.sub, self.backups)
        self.assertTrue(self.sub.read_bytes().startswith(UTF8_BOM))
        self.assertEqual(srt.read_srt(self.sub)[0].text, KOREAN[0])

    def test_a_utf16_srt_is_written_back_as_utf8_with_a_byte_order_mark(self):
        self.sub.write_bytes(srt_of(STARTS, KOREAN).encode("utf-16"))
        self.assertEqual(self.repairer.repair(self.sub, self.backups).status, Status.FIXED)
        self.assertTrue(self.sub.read_bytes().startswith(UTF8_BOM))

    def test_a_file_that_changed_while_it_was_checked_is_left_alone_and_checked_again_later(self):
        real_align = Repairer.align

        def align_then_edit(repairer, *args):
            result = real_align(repairer, *args)
            self.sub.write_text(srt_of(STARTS, [f"편집 {t}" for t in KOREAN]), encoding="utf-8")
            return result

        with mock.patch.object(Repairer, "align", align_then_edit):
            verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual((verdict.status, verdict.note), (Status.SKIP, "changed while it was being checked"))
        self.assertFalse(verdict.cacheable)
        self.assertEqual(self.backed_up(), [])

    def test_a_failed_write_leaves_the_original_and_its_backup(self):
        if not hard_links_work(self.tmp):
            self.skipTest("no hard links here: backups would use the replace that this test makes fail")
        original = self.sub.read_bytes()
        with mock.patch.object(fileio.os, "replace", side_effect=PermissionError(13, "in use")):
            verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.ERROR)
        self.assertIn("in use", verdict.note)
        self.assertIn("PermissionError", verdict.detail)
        self.assertEqual(self.sub.read_bytes(), original)
        self.assertEqual([p.read_bytes() for p in self.backed_up()], [original])

    def test_a_backup_that_cannot_be_written_stops_the_repair(self):
        original = self.sub.read_bytes()
        with failing_backups(self):
            verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.ERROR)
        self.assertEqual(self.sub.read_bytes(), original)

    def test_a_read_only_subtitle_is_refused_and_leaves_no_temporary_file(self):
        os.chmod(self.sub, stat.S_IREAD)
        self.addCleanup(os.chmod, self.sub, stat.S_IREAD | stat.S_IWRITE)
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.ERROR)
        self.assertIn("read-only", verdict.note)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["Show S01E01.ko.srt", "Show S01E01.mkv"])


def failing_backups(case, suffix=""):
    """Make the writes under the case's backup folder (of files ending in `suffix`) fail, and only those."""
    real = fileio.atomic_write_bytes
    root = os.path.normcase(str(case.backups.root))

    def write(path, data, **kwargs):
        if os.path.normcase(str(path)).startswith(root) and str(path).endswith(suffix):
            raise OSError(28, "disk full")
        return real(path, data, **kwargs)

    return mock.patch.object(writers, "atomic_write_bytes", write)


class Copies(LibraryCase):
    def test_the_correction_goes_to_a_new_file_and_the_original_is_untouched(self):
        dest = self.dir / "Show S01E01.ko.synced.srt"
        original = self.sub.read_bytes()
        verdict = self.repairer.copy(self.sub, dest)
        self.assertEqual((verdict.status, verdict.output), (Status.FIXED, dest))
        self.assertIn("original untouched", verdict.note)
        self.assertEqual(self.starts_of(dest), [s + 5.0 for s in STARTS])
        self.assertEqual(self.sub.read_bytes(), original)
        self.assertEqual(self.backed_up(), [])

    def test_an_existing_file_is_never_overwritten(self):
        dest = self.write("Show S01E01.ko.synced.srt", "previous result")
        verdict = self.repairer.copy(self.sub, dest)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertIn("already exists", verdict.note)
        self.assertEqual(dest.read_text(), "previous result")
        self.assertEqual(self.aligner.calls, [])

    def test_a_file_with_the_expected_signature_is_replaced_but_not_one_that_changed(self):
        dest = self.write("Show S01E01.ko.synced.srt", "our earlier copy")
        own = fileio.signature(dest)
        self.assertEqual(self.repairer.copy(self.sub, dest, replaceable=own).status, Status.FIXED)
        self.assertEqual(self.starts_of(dest), [s + 5.0 for s in STARTS])
        dest.write_text("edited by someone else, longer than before")
        self.assertEqual(self.repairer.copy(self.sub, dest, replaceable=own).status, Status.SKIP)
        self.assertEqual(dest.read_text(), "edited by someone else, longer than before")

    def test_a_copy_that_appears_during_the_check_is_not_overwritten(self):
        dest = self.dir / "late.ko.synced.srt"
        real_align = Repairer.align

        def align_then_create(repairer, *args):
            result = real_align(repairer, *args)
            dest.write_text("someone else's file")
            return result

        with mock.patch.object(Repairer, "align", align_then_create):
            verdict = self.repairer.copy(self.sub, dest)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertEqual(dest.read_text(), "someone else's file")

    @unittest.skipIf(os.name == "nt", "creating symlinks needs a privilege on Windows")
    def test_a_dangling_link_in_the_copys_place_is_not_followed(self):
        target = self.tmp / "outside" / "planted.srt"
        target.parent.mkdir()
        dest = self.dir / "Show S01E01.ko.synced.srt"
        dest.symlink_to(target)
        verdict = self.repairer.copy(self.sub, dest)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertFalse(target.exists())

    def test_a_copy_whose_folder_stops_being_allowed_is_not_written(self):
        dest = self.dir / "Show S01E01.ko.synced.srt"
        verdict = self.repairer.copy(self.sub, dest, allowed=lambda path: False)
        self.assertEqual(verdict.status, Status.ERROR)
        self.assertFalse(dest.exists())

    def test_a_copy_whose_folder_was_swapped_during_the_write_is_taken_back(self):
        dest = self.dir / "Show S01E01.ko.synced.srt"
        answers = iter([True, False])
        verdict = self.repairer.copy(self.sub, dest, allowed=lambda path: next(answers))
        self.assertEqual(verdict.status, Status.ERROR)
        self.assertFalse(dest.exists())


class VerdictKey(LibraryCase):
    def key(self):
        return verdict_key(self.sub, sorted(self.dir.iterdir()))

    def test_the_key_covers_the_subtitle_its_video_and_its_sibling_subtitles(self):
        key = self.key()
        reference = self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        with_reference = self.key()
        self.assertNotEqual(with_reference, key)
        reference.write_text(srt_of(STARTS, JAPANESE[::-1]) + "\n", encoding="utf-8")
        self.assertNotEqual(self.key(), with_reference)
        before_video = self.key()
        self.video.write_bytes(b"a replaced video")
        self.assertNotEqual(self.key(), before_video)

    def test_an_ass_sidecar_counts_too(self):
        key = self.key()
        self.write("Show S01E01.ko.ass", ASS_HEADER + ass_events(STARTS, KOREAN))
        self.assertNotEqual(self.key(), key)

    def test_files_of_another_video_and_the_servers_copies_do_not_change_the_key(self):
        key = self.key()
        self.write("Show S01E02.mkv", b"x")
        self.write("Show S01E02.ja.srt", srt_of(STARTS, JAPANESE))
        self.write("Show S01E01.ko.synced.srt", srt_of(STARTS, KOREAN))
        self.assertEqual(self.key(), key)


class AssAlone(LibraryCase):
    SUBTITLE = "Show S01E01.ko.ass"

    def setUp(self):
        super().setUp()
        self.signs = [12.0, 13.0, 47.0]
        self.sub.write_text(ASS_HEADER + ass_events(STARTS, KOREAN)
                            + ass_events(self.signs, ["간판 1", "간판 2", "간판 3"], extra="{\\pos(10,10)}")
                            + ass_events([3.0], ["노래"], style="OP Romaji")
                            + ass_events([5.0], ["가라오케"], extra="{\\k20}"), encoding="utf-8")

    def test_its_dialogue_is_shifted_and_everything_else_is_kept(self):
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.FIXED)
        self.assertIn("dialogue events shifted", verdict.note)
        text = self.sub.read_text(encoding="utf-8")
        self.assertIn("Dialogue: 0,0:00:05.00,0:00:06.50,Default", text)
        self.assertIn("Style: Default,Arial,20", text)
        self.assertEqual(len(self.backed_up()), 1)

    def test_signs_karaoke_and_song_styles_do_not_take_part_in_the_alignment(self):
        self.repairer.measure(self.sub)
        _, work = self.aligner.calls[0]
        self.assertEqual([c.text for c in work], KOREAN)

    def test_times_with_three_digit_fractions_are_shifted(self):
        self.sub.write_text(ASS_HEADER + "".join(
            f"Dialogue: 0,0:{int(s) // 60:02d}:{int(s) % 60:02d}.250,0:{int(s) // 60:02d}:{int(s) % 60 + 1:02d}.500,"
            f"Default,,0,0,0,,{t}\n" for s, t in zip(STARTS, KOREAN)), encoding="utf-8")
        self.assertEqual(self.repairer.repair(self.sub, self.backups).status, Status.FIXED)
        self.assertIn("Dialogue: 0,0:00:05.25,0:00:06.50,Default", self.sub.read_text(encoding="utf-8"))

    def test_an_ass_with_event_times_it_cannot_read_is_left_alone(self):
        unreadable = self.sub.read_text(encoding="utf-8") + "Dialogue: 0,0:00:10,0:00:11,Default,,0,0,0,,시간 없음\n"
        self.sub.write_text(unreadable, encoding="utf-8")
        events = CUES + len(self.signs) + 2 + 1
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertIn(f"1 of its {events} events have times this tool cannot read", verdict.note)
        self.assertEqual(self.sub.read_text(encoding="utf-8"), unreadable)

    def test_events_the_shift_would_move_before_zero_leave_the_file_alone(self):
        self.sub.write_text(ASS_HEADER + ass_events([s + 10.0 for s in STARTS], KOREAN)
                            + ass_events([2.0], ["간판"], extra="{\\pos(10,10)}"), encoding="utf-8")
        before = self.sub.read_bytes()
        self.aligner.shift = -5.0
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertIn("would start before 0:00", verdict.note)
        self.assertEqual(self.sub.read_bytes(), before)

    def test_a_backup_that_cannot_be_written_leaves_the_ass_alone(self):
        before = self.sub.read_bytes()
        with failing_backups(self):
            self.assertEqual(self.repairer.repair(self.sub, self.backups).status, Status.ERROR)
        self.assertEqual(self.sub.read_bytes(), before)


class AssTwins(LibraryCase):
    def ass(self, starts=STARTS, raw=None):
        return self.write("Show S01E01.ko.ass", raw if raw is not None else ASS_HEADER + ass_events(starts, KOREAN)
                          + "Comment: 0,0:00:20.00,0:00:21.00,Default,,0,0,0,,주석 안녕하세요\n")

    def test_the_twin_is_shifted_together_with_the_srt_and_both_are_backed_up(self):
        ass_file = self.ass()
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.FIXED)
        self.assertIn("+ .ass", verdict.note)
        text = ass_file.read_text(encoding="utf-8")
        self.assertIn("Dialogue: 0,0:00:15.00,0:00:16.50", text)
        self.assertIn("Comment: 0,0:00:20.00,0:00:21.00", text)
        self.assertEqual(sorted(p.suffix for p in self.backed_up()), [".ass", ".srt"])

    def test_the_twin_takes_the_shift_of_each_piece(self):
        ass_file = self.ass()
        self.aligner.shift = lambda s: 5.0 + s / 1000
        self.repairer.repair(self.sub, self.backups)
        self.assertIn(f"Dialogue: 0,{ass.ass_time(STARTS[-1] + 5.0)},", ass_file.read_text(encoding="utf-8"))

    def test_a_twin_with_its_own_timing_is_left_alone(self):
        ass_file = self.ass(starts=[s + 3.0 for s in STARTS])
        before = ass_file.read_bytes()
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.FIXED)
        self.assertIn("the .ass was left alone: its timing does not match the .srt", verdict.note)
        self.assertEqual(ass_file.read_bytes(), before)

    def test_an_ass_follows_its_preferred_srt_only(self):
        ass_file = self.ass()
        other = self.write("Show S01E01.ko.hi.srt", srt_of(STARTS, KOREAN))
        self.aligner.shift = 7.0
        self.assertEqual(self.repairer.repair(other, self.backups).status, Status.FIXED)
        self.assertIn("Dialogue: 0,0:00:10.00,0:00:11.50", ass_file.read_text(encoding="utf-8"))
        self.aligner.shift = 5.0
        self.assertEqual(self.repairer.repair(self.sub, self.backups).status, Status.FIXED)
        self.assertIn("Dialogue: 0,0:00:15.00,0:00:16.50", ass_file.read_text(encoding="utf-8"))

    def test_both_files_or_neither_when_the_srt_cannot_be_written(self):
        ass_file = self.ass()
        originals = ass_file.read_bytes(), self.sub.read_bytes()
        real = fileio.atomic_write_bytes

        def fail_for_the_srt(path, data, **kwargs):
            if Path(path) == self.sub:
                raise PermissionError(13, "in use")
            return real(path, data, **kwargs)

        with mock.patch.object(writers, "atomic_write_bytes", fail_for_the_srt):
            verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.ERROR)
        self.assertEqual((ass_file.read_bytes(), self.sub.read_bytes()), originals)

    def test_when_the_twin_cannot_be_put_back_the_error_names_the_backups_and_is_kept(self):
        ass_file = self.ass()
        real = fileio.atomic_write_bytes
        ass_writes = []

        def srt_and_restore_fail(path, data, **kwargs):
            if Path(path) == self.sub:
                raise PermissionError(13, "in use")
            if Path(path) == ass_file:
                ass_writes.append(data)
                if len(ass_writes) > 1:
                    raise PermissionError(13, "in use")
            return real(path, data, **kwargs)

        with mock.patch.object(writers, "atomic_write_bytes", srt_and_restore_fail):
            verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.ERROR)
        self.assertIn("restore it from the backup", verdict.note)
        self.assertIn(str(self.backups.path_for(ass_file)), verdict.detail)
        self.assertTrue(verdict.cacheable)

    def test_a_backup_that_cannot_be_written_leaves_both_files_alone(self):
        ass_file = self.ass()
        originals = ass_file.read_bytes(), self.sub.read_bytes()
        for suffix in ("", ".ass"):
            with failing_backups(self, suffix):
                self.assertEqual(self.repairer.repair(self.sub, self.backups).status, Status.ERROR, suffix)
            self.assertEqual((ass_file.read_bytes(), self.sub.read_bytes()), originals, suffix)

    def test_a_twin_that_is_a_link_is_left_alone(self):
        outside = self.write("x.ass", ASS_HEADER + ass_events(STARTS, KOREAN), folder=self.tmp / "outside")
        make_file_link(self.dir / "Show S01E01.ko.ass", outside)
        before = outside.read_bytes()
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.FIXED)
        self.assertIn("the .ass was left alone: it is a link", verdict.note)
        self.assertEqual(outside.read_bytes(), before)

    def test_an_ass_its_srt_would_leave_alone_does_not_step_aside_for_it(self):
        ass_file = self.ass()
        self.assertEqual(self.repairer.measure(ass_file, preview=True).status, Status.SKIP)
        with mock.patch.object(writers, "is_link", lambda path: Path(path) == ass_file):
            verdict = self.repairer.measure(ass_file, preview=True)
        self.assertEqual(verdict.status, Status.WOULD_FIX)
        self.assertNotIn("fixed together", verdict.note)

    def test_a_twin_that_does_not_decode_is_left_alone_but_the_srt_is_still_fixed(self):
        ass_file = self.ass(raw=UNDECODABLE)
        verdict = self.repairer.repair(self.sub, self.backups)
        self.assertEqual(verdict.status, Status.FIXED)
        self.assertIn("does not decode", verdict.note)
        self.assertEqual(ass_file.read_bytes(), UNDECODABLE)
        self.assertEqual(self.starts_of(self.sub), [s + 5.0 for s in STARTS])

    def test_a_cp949_twin_is_written_back_as_utf8_with_a_byte_order_mark(self):
        ass_file = self.ass(raw=(ASS_HEADER + ass_events(STARTS, KOREAN)).encode("cp949"))
        self.assertEqual(self.repairer.repair(self.sub, self.backups).status, Status.FIXED)
        self.assertTrue(ass_file.read_bytes().startswith(UTF8_BOM))
        self.assertIn(KOREAN[0], ass_file.read_text(encoding="utf-8-sig"))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is not installed")
class Sami(LibraryCase):
    def setUp(self):
        super().setUp()
        self.sub.unlink()
        self.smi = self.write("Show S01E01.ko.smi", self.sami().encode("cp949"))

    @staticmethod
    def sami(classes=("KRCC",)):
        texts = {"KRCC": KOREAN, "ENCC": ENGLISH, "XXCC": ENGLISH}
        langs = {"KRCC": "ko-KR", "ENCC": "en-US", "XXCC": "xx"}
        style = "".join(f".{c} {{Name:x; lang:{langs[c]}; SAMIType:CC;}}\n" for c in classes)
        body = "".join(f"<SYNC Start={int(s * 1000)}><P Class={c}>{texts[c][i]}\n"
                       f"<SYNC Start={int(s * 1000 + 1500)}><P Class={c}>&nbsp;\n"
                       for i, s in enumerate(STARTS) for c in classes)
        return f"<SAMI><HEAD><STYLE TYPE=\"text/css\"><!--\n{style}--></STYLE></HEAD><BODY>\n{body}</BODY></SAMI>\n"

    def test_a_smi_becomes_an_srt_and_the_smi_goes_to_the_backup(self):
        verdict = self.repairer.repair(self.smi, self.backups)
        self.assertEqual(verdict.status, Status.FIXED)
        self.assertIn(".smi -> .srt", verdict.note)
        self.assertEqual(verdict.output, self.dir / "Show S01E01.ko.srt")
        self.assertFalse(self.smi.exists())
        self.assertEqual(self.starts_of(self.dir / "Show S01E01.ko.srt"), [s + 5.0 for s in STARTS])
        self.assertEqual([p.name for p in self.backed_up()], ["Show S01E01.ko.smi"])

    def test_the_srt_of_a_legacy_smi_starts_with_a_byte_order_mark(self):
        self.repairer.repair(self.smi, self.backups)
        self.assertTrue((self.dir / "Show S01E01.ko.srt").read_bytes().startswith(UTF8_BOM))

    def test_each_other_language_of_a_smi_gets_its_own_srt(self):
        self.smi.write_bytes(self.sami(("KRCC", "ENCC")).encode("utf-8"))
        verdict = self.repairer.repair(self.smi, self.backups)
        self.assertEqual(verdict.status, Status.FIXED)
        self.assertIn("also Show S01E01.en.srt", verdict.note)
        self.assertEqual([c.text for c in srt.read_srt(self.dir / "Show S01E01.ko.srt")], KOREAN)
        english = srt.read_srt(self.dir / "Show S01E01.en.srt")
        self.assertEqual(([c.text for c in english], [c.start for c in english]), (ENGLISH[:CUES], STARTS))
        self.assertFalse(self.smi.exists())

    def test_a_smi_whose_other_language_cannot_get_its_own_file_is_kept(self):
        self.smi.write_bytes(self.sami(("KRCC", "ENCC")).encode("utf-8"))
        taken = self.write("Show S01E01.en.srt", "someone's English file")
        verdict = self.repairer.repair(self.smi, self.backups)
        self.assertEqual(verdict.status, Status.FIXED)
        self.assertIn("the .smi was kept: Show S01E01.en.srt already exists", verdict.note)
        self.assertTrue(self.smi.exists())
        self.assertEqual(taken.read_text(), "someone's English file")
        self.assertTrue((self.dir / "Show S01E01.ko.srt").exists())

    def test_a_smi_with_a_class_in_no_known_language_is_kept(self):
        self.smi.write_bytes(self.sami(("KRCC", "XXCC")).encode("utf-8"))
        verdict = self.repairer.repair(self.smi, self.backups)
        self.assertIn("the .smi was kept: XXCC is in no known language", verdict.note)
        self.assertTrue(self.smi.exists())

    def test_a_smi_with_several_languages_none_of_them_its_own_is_left_alone(self):
        self.smi.write_bytes(self.sami(("KRCC", "ENCC")).replace("lang:ko-KR", "lang:en-GB").replace("KRCC", "XXCC")
                             .encode("utf-8"))
        verdict = self.repairer.repair(self.smi, self.backups)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertIn("none of them is Korean", verdict.note)
        self.assertTrue(self.smi.exists())

    def test_a_smi_next_to_an_srt_in_its_language_is_left_alone(self):
        for name in ("Show S01E01.ko.srt", "Show S01E01.ko.hi.srt"):
            existing = self.write(name, srt_of(STARTS, KOREAN))
            verdict = self.repairer.repair(self.smi, self.backups)
            self.assertEqual(verdict.status, Status.SKIP, name)
            self.assertIn(f"{name} already exists next to the .smi", verdict.note)
            self.assertTrue(self.smi.exists())
            existing.unlink()

    def test_a_smi_whose_srt_name_is_taken_by_a_file_that_is_no_twin_is_left_alone(self):
        smi = self.write("Show S01E01.ko.synced.smi", self.sami().encode("cp949"))
        self.write("Show S01E01.ko.synced.srt", srt_of(STARTS, KOREAN))
        verdict = self.repairer.repair(smi, self.backups)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertIn("Show S01E01.ko.synced.srt already exists next to the .smi", verdict.note)

    def test_a_dangling_link_in_the_place_of_the_srt_is_not_followed(self):
        planted = self.tmp / "outside" / "planted.srt"
        planted.parent.mkdir()
        make_file_link(self.dir / "Show S01E01.ko.srt", planted)
        verdict = self.repairer.repair(self.smi, self.backups)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertIn("Show S01E01.ko.srt already exists next to the .smi", verdict.note)
        self.assertFalse(planted.exists())
        self.assertTrue(self.smi.exists())

    def test_a_link_that_appears_in_the_place_of_the_srt_during_the_check_is_not_followed(self):
        planted = self.tmp / "outside" / "planted.srt"
        planted.parent.mkdir()
        real_keep = type(self.backups).keep

        def keep_then_plant(backups, path, data):
            result = real_keep(backups, path, data)
            make_file_link(self.dir / "Show S01E01.ko.srt", planted)
            return result

        with mock.patch.object(type(self.backups), "keep", keep_then_plant):
            verdict = self.repairer.repair(self.smi, self.backups)
        self.assertEqual(verdict.status, Status.ERROR)
        self.assertFalse(planted.exists())
        self.assertTrue(self.smi.exists())

    def test_the_hook_leaves_a_smi_in_place(self):
        before = self.smi.read_bytes()
        verdict = self.repairer.repair(self.smi, self.backups, convert_smi=False)
        self.assertEqual((verdict.status, verdict.cacheable), (Status.SKIP, False))
        self.assertIn("the hook leaves a .smi in place", verdict.note)
        self.assertEqual(self.smi.read_bytes(), before)
        self.assertFalse((self.dir / "Show S01E01.ko.srt").exists())

    def test_an_srt_that_appears_while_the_smi_is_converted_is_not_overwritten(self):
        real_keep = type(self.backups).keep

        def keep_then_appear(backups, path, data):
            result = real_keep(backups, path, data)
            (self.dir / "Show S01E01.ko.srt").write_bytes(b"someone else's file")
            return result

        with mock.patch.object(type(self.backups), "keep", keep_then_appear):
            verdict = self.repairer.repair(self.smi, self.backups)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertIn("appeared", verdict.note)
        self.assertEqual((self.dir / "Show S01E01.ko.srt").read_bytes(), b"someone else's file")
        self.assertTrue(self.smi.exists())

    def test_a_smi_that_cannot_be_removed_is_rolled_back_and_checked_again_later(self):
        real_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == self.smi:
                raise PermissionError(13, "in use")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", unlink):
            verdict = self.repairer.repair(self.smi, self.backups)
        self.assertEqual(verdict.status, Status.ERROR)
        self.assertIn("could not remove", verdict.note)
        self.assertFalse(verdict.cacheable)
        self.assertTrue(self.smi.exists())
        self.assertFalse((self.dir / "Show S01E01.ko.srt").exists(), "no second track may be left behind")

    def test_a_read_only_smi_gives_a_read_only_srt_or_on_windows_stays_as_it_was(self):
        os.chmod(self.smi, stat.S_IREAD)
        self.addCleanup(lambda: self.smi.exists() and os.chmod(self.smi, stat.S_IREAD | stat.S_IWRITE))
        srt_file = self.dir / "Show S01E01.ko.srt"
        self.addCleanup(lambda: srt_file.exists() and os.chmod(srt_file, stat.S_IREAD | stat.S_IWRITE))
        verdict = self.repairer.repair(self.smi, self.backups)
        if os.name == "nt":             # Windows refuses to remove a read-only file
            self.assertEqual((verdict.status, self.smi.exists(), srt_file.exists()), (Status.ERROR, True, False))
        else:
            self.assertEqual((verdict.status, self.smi.exists()), (Status.FIXED, False))
            self.assertFalse(srt_file.stat().st_mode & stat.S_IWUSR)

    def test_an_english_only_smi_named_as_korean_is_refused(self):
        self.smi.write_bytes(b"<SAMI><BODY><SYNC Start=1000><P Class=ENCC>hello</BODY></SAMI>")
        verdict = self.repairer.repair(self.smi, self.backups)
        self.assertEqual(verdict.status, Status.SKIP)
        self.assertIn("cannot decode", verdict.note)


class ReferenceChoice(LibraryCase):
    def measure(self, settings=None):
        return Repairer(settings or self.settings, self.media, FakeAligner(), FakeFfsubsync()).measure(self.sub)

    def test_a_reference_track_needs_enough_cues(self):
        self.media.tracks = {"en": [track(references.MIN_REFERENCE_CUES - 1)]}
        self.assertIn("no reference", self.measure().note)
        self.media.tracks = {"en": [track(references.MIN_REFERENCE_CUES)]}
        self.assertEqual(self.measure().reference, "embedded en")

    def test_the_first_embedded_language_in_the_order_wins_over_a_fuller_track(self):
        self.media.tracks = {"en": [track(90)], "ja": [track(55, JAPANESE)]}
        settings = make_settings(self.root, self.data, "ko", embedded_reference_languages=["ja", "en"])
        self.assertEqual(self.measure(settings).reference, "embedded ja")

    def test_by_default_english_comes_first_then_the_fullest_track_of_any_other_language(self):
        self.media.tracks = {"ja": [track(70, JAPANESE)], "de": [track(90, GERMAN)]}
        self.assertEqual(self.measure().reference, "embedded de")
        self.media.tracks["en"] = [track(55)]
        self.assertEqual(self.measure().reference, "embedded en")

    def test_any_other_language_never_means_the_subtitles_own(self):
        self.media.tracks = {"ko": [track(90, KOREAN)], "ja": [track(55, JAPANESE)]}
        self.assertEqual(self.measure().reference, "embedded ja")


if __name__ == "__main__":
    unittest.main()
