"""Tests for subsync_repair/engine/policy.py: when a correction is believable. The shapes come from real alass runs."""
import unittest

from subsync_repair.engine import policy
from subsync_repair.engine.policy import Correction
from support import another_episode_shift, starts_with_gap


def correction(starts, shift_of):
    """The Correction that moves cue i (starting at s) by shift_of(i, s)."""
    return Correction(starts, [s + shift_of(i, s) for i, s in enumerate(starts)])


def problem(fixed, original=None, end=None, duration=None):
    original = fixed if original is None else original
    return policy.result_problem(Correction([float(s) for s in original], [float(s) for s in fixed]), end, duration)


class Pieces(unittest.TestCase):
    def test_a_constant_shift_is_one_piece_that_starts_at_the_first_cue(self):
        self.assertEqual(Correction([50, 60, 70], [53, 63, 73]).pieces, ((50, 3),))

    def test_a_new_piece_starts_where_the_shift_changes_by_more_than_the_tolerance(self):
        self.assertEqual(Correction([1, 5, 9, 20], [3, 7, 4, 15]).pieces, ((1, 2), (9, -5)))

    def test_the_tolerance_itself_still_belongs_to_the_piece(self):
        self.assertEqual(policy.runs_of([0.0, policy.PIECE_TOLERANCE]), [(0, 2)])
        self.assertEqual(policy.runs_of([0.0, policy.PIECE_TOLERANCE + 2 ** -10]), [(0, 1), (1, 1)])

    def test_a_piece_is_measured_from_its_first_cue_not_from_the_last(self):
        self.assertEqual(policy.runs_of([0.0, 0.3, 0.6, 0.9]), [(0, 2), (2, 2)])

    def test_lists_of_different_lengths_are_a_programming_error(self):
        with self.assertRaises(ValueError):
            Correction([1, 2, 3], [2, 3])

    def test_describe_names_where_each_piece_starts(self):
        self.assertEqual(policy.describe(((95.0, 2.04), (159.6, -7.0), (200.0, 1.0))),
                         "shifts 1:35 +2.0s, 2:40 -7.0s, 3:20 +1.0s")


class InSync(unittest.TestCase):
    def test_every_cue_counts_not_only_the_first_of_each_piece(self):
        drifting = Correction([0.0, 10.0, 20.0], [0.5, 10.85, 20.5])
        self.assertEqual(drifting.pieces, ((0.0, 0.5),))
        self.assertFalse(policy.in_sync(drifting))

    def test_the_threshold_itself_is_in_sync(self):
        self.assertTrue(policy.in_sync(Correction([0.0, 0.0], [policy.THRESHOLD, 0.0])))
        self.assertFalse(policy.in_sync(Correction([0.0, 0.0], [policy.THRESHOLD + 2 ** -10, 0.0])))


class RealShapes(unittest.TestCase):
    """What alass did to real subtitles, against the video's own English track."""

    def assert_fixable(self, found):
        self.assertFalse(policy.in_sync(found))
        self.assertFalse(policy.mismatch(found))
        self.assertIsNone(policy.problem(found, found.fixed[-1] + 1.5, None))

    def test_a_subtitle_of_another_episode_is_a_mismatch(self):
        starts = [10.0 * i for i in range(264)]
        found = correction(starts, lambda i, s: another_episode_shift(i, s, starts))
        self.assertGreater(len(found.pieces), policy.MAX_SEGMENTS)
        self.assertTrue(policy.mismatch(found))

    def test_a_cold_open_that_drifted_is_fixable(self):
        self.assert_fixable(correction(starts_with_gap(306, after=25, gap=15.0), lambda i, s: 10.17 if i < 26 else 0.0))

    def test_a_block_in_the_middle_that_drifted_is_fixable(self):
        self.assert_fixable(correction(starts_with_gap(264, after=170, gap=30.0),
                                       lambda i, s: 18.0 if 136 <= i <= 170 else 0.1))

    def test_a_subtitle_late_by_the_same_large_amount_throughout_is_fixable(self):
        self.assert_fixable(correction(starts_with_gap(264, after=0, gap=0.0), lambda i, s: 45.0))

    def test_an_opening_missing_after_the_first_part_is_fixable(self):
        self.assert_fixable(correction(starts_with_gap(264, after=5, gap=3.0), lambda i, s: 0.0 if i < 6 else 90.0))

    def test_a_frame_rate_difference_is_refused_as_one(self):
        starts = [2.0 * i for i in range(700)]
        found = correction(starts, lambda i, s: s * (25 / 23.976 - 1))
        self.assertFalse(policy.mismatch(found))
        self.assertEqual(policy.correction_problem(found), policy.FRAME_RATE)


class Mismatch(unittest.TestCase):
    STARTS = [5.0 * i for i in range(264)]

    @staticmethod
    def scattered(i):
        return 40.0 if i % 2 else -40.0

    def test_a_piece_that_holds_the_kept_share_of_the_cues_is_enough(self):
        kept = int(len(self.STARTS) * policy.KEPT_SHARE)
        self.assertFalse(policy.mismatch(correction(self.STARTS, lambda i, s: 0.0 if i < kept else self.scattered(i))))
        self.assertTrue(policy.mismatch(correction(self.STARTS,
                                                   lambda i, s: 0.0 if i < kept - 1 else self.scattered(i))))

    def test_only_a_shift_beyond_the_limit_can_be_a_mismatch(self):
        limit = policy.MISMATCH_SHIFT
        self.assertFalse(policy.mismatch(correction(self.STARTS, lambda i, s: limit if i % 2 else -limit)))
        self.assertTrue(policy.mismatch(correction(self.STARTS, lambda i, s: (limit if i % 2 else -limit) * 1.01)))


class CorrectionShape(unittest.TestCase):
    @staticmethod
    def pieces(shifts, every=40.0):
        """A Correction whose pieces start `every` seconds apart, ten cues each."""
        starts = [every * p + i for p in range(len(shifts)) for i in range(10)]
        return correction(starts, lambda i, s: shifts[i // 10])

    def test_the_largest_shift_allowed_is_exact(self):
        self.assertIsNone(policy.correction_problem(self.pieces([policy.MAX_SHIFT])))
        self.assertEqual(policy.correction_problem(self.pieces([policy.MAX_SHIFT + 0.5])), "implausible correction")

    def test_many_pieces_may_not_move_far(self):
        alternating = [10.0 + 10.0 * (i % 2) for i in range(policy.MAX_SEGMENTS + 1)]
        self.assertEqual(policy.correction_problem(self.pieces(alternating)), "implausible correction")
        self.assertIsNone(policy.correction_problem(self.pieces(alternating[:policy.MAX_SEGMENTS])))
        small = [policy.SLIDE_OK - 1.0 * (i % 2) for i in range(policy.MAX_SEGMENTS + 1)]
        self.assertIsNone(policy.correction_problem(self.pieces(small)))

    def test_a_start_that_keeps_changing_is_refused(self):
        self.assertIn("keeps changing", policy.correction_problem(self.pieces([0.9, 4.0, 6.3, 9.1], every=5.0)))


class UnstableStart(unittest.TestCase):
    def test_a_shift_that_ramps_up_right_after_the_first_cue_is_refused(self):
        self.assertTrue(policy.unstable_start([(0.0, -0.9), (4.0, -4.0), (6.0, -6.3), (9.0, -9.1)]))
        self.assertTrue(policy.unstable_start([(0.0, -0.6), (5.0, -4.7), (16.0, -9.0)]))

    def test_the_window_starts_at_the_first_cue_not_at_zero(self):
        self.assertTrue(policy.unstable_start([(95.0, -0.9), (99.0, -4.0), (101.0, -6.3)]))

    def test_a_piece_exactly_at_the_end_of_the_window_is_outside_it(self):
        self.assertFalse(policy.unstable_start([(10.0, 0.9), (20.0, 4.0), (10.0 + policy.EARLY_WINDOW, 6.3)]))
        self.assertTrue(policy.unstable_start([(10.0, 0.9), (20.0, 4.0), (9.5 + policy.EARLY_WINDOW, 6.3)]))

    def test_one_early_change_is_a_normal_cut_or_intro(self):
        self.assertFalse(policy.unstable_start([(0.0, 0.9), (12.0, -9.0)]))
        self.assertFalse(policy.unstable_start([(0.0, -9.0)]))
        self.assertFalse(policy.unstable_start([]))

    def test_many_changes_later_in_the_file_do_not_count(self):
        self.assertFalse(policy.unstable_start([(0.0, 0.9), (228.0, -8.9), (300.0, -9.0), (410.0, -2.0)]))

    def test_a_gentle_frame_rate_drift_is_not_instability(self):
        self.assertFalse(policy.unstable_start([(0.0, 0.4), (9.0, 0.8), (17.5, 1.2), (26.0, 1.6)]))
        self.assertFalse(policy.unstable_start([(0.0, -0.4), (9.0, -0.8), (17.5, -1.2)]))

    def test_a_step_of_exactly_the_gentle_limit_is_still_gentle(self):
        self.assertFalse(policy.unstable_start([(0.0, 0.0), (5.0, policy.GENTLE_STEP), (10.0, 2 * policy.GENTLE_STEP)]))
        self.assertTrue(policy.unstable_start([(0.0, 0.0), (5.0, policy.GENTLE_STEP), (10.0, 2.25)]))

    def test_small_steps_that_change_direction_are_unstable(self):
        self.assertTrue(policy.unstable_start([(0.0, 0.4), (9.0, 0.8), (17.5, 0.3)]))


class ResultProblem(unittest.TestCase):
    def test_a_normal_result_passes(self):
        self.assertIsNone(problem([1, 3, 5, 8], [0, 2, 4, 7]))

    def test_reordered_cues_are_refused_unless_the_original_already_was(self):
        self.assertEqual(problem([1, 5, 3, 8], [1, 3, 5, 8]), "cue order changed")
        self.assertIsNone(problem([1, 5, 3, 8], [1, 6, 4, 9]))

    def test_the_time_zero_limit_is_exact(self):
        self.assertIsNone(problem([0, 0, 0, 7, 9], [5, 5, 5, 7, 9]))                # three more at zero is allowed
        self.assertEqual(problem([0, 0, 0, 0, 9], [5, 5, 5, 5, 9]), "many cues pushed to time zero")
        self.assertIsNone(problem([0, 0, 0, 0, 4], [0, 0, 0, 0, 5]))

    def test_a_fix_that_stacks_cues_on_one_start_time_is_refused(self):
        original = [1, 3, 5, 7, 9, 11, 13]
        self.assertEqual(problem([3, 3, 3, 7, 9, 11, 13], original), "2 cues stacked onto shared start times")
        self.assertIsNone(problem([3, 3, 5, 7, 9, 11, 13], original))
        self.assertIsNone(problem([4, 4, 4, 8, 10, 12, 14], [3, 3, 3, 7, 9, 11, 13]))

    def test_the_end_of_the_video_rule(self):
        self.assertIsNone(problem([1, 3], end=100 + policy.END_SLACK, duration=100.0))
        self.assertIn("run past the end", problem([1, 3], end=100 + policy.END_SLACK + 0.1, duration=100.0))
        self.assertIsNone(problem([1, 3], end=500, duration=None))
        self.assertIsNone(problem([], end=None, duration=100.0))


if __name__ == "__main__":
    unittest.main()
