"""Tests for subsync_repair/external/audio_check.py and ffsubsync_check.py: checking a reference
against the audio with ffsubsync."""
import io
import json
import subprocess
import sys
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from subsync_repair.external import audio_check, ffsubsync_check, process
from subsync_repair.external.audio_check import Ffsubsync
from support import TempFolder, completed


def result_line(ok=True, offset=0.25, scale=1.0, code=0):
    return "RESULT " + json.dumps({"retval": code, "sync_was_successful": ok, "offset_seconds": offset,
                                   "framerate_scale_factor": scale})


def ffsubsync_output(score=1234.5, offset=0.25, scale=1.0, ok=True, code=0):
    return completed(stdout=result_line(ok, offset, scale, code), stderr=f"INFO:ffsubsync:score: {score:.3f}\n",
                     code=code)


class FfsubsyncAdapter(unittest.TestCase):
    def check(self, reply=None, error=None, python="py.exe"):
        with mock.patch.object(process, "run", side_effect=error, return_value=reply) as run:
            return Ffsubsync(python, ["/libs"]).check("v.mkv", "r.srt"), run

    def test_a_reference_that_needs_almost_no_correction_is_good(self):
        result, run = self.check(ffsubsync_output())
        self.assertEqual(result, (True, "reference offset +0.25s scale 1.000", True, ""))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["py.exe", "-I", audio_check.CHECK_SCRIPT])
        self.assertEqual(json.loads(cmd[3]), ["/libs"])

    def test_an_offset_or_a_scale_beyond_the_limits_is_a_lasting_no(self):
        for reply in (ffsubsync_output(offset=-2.5), ffsubsync_output(scale=1.0427)):
            result, _ = self.check(reply)
            self.assertEqual((result.ok, result.lasting), (False, True))
        self.assertTrue(self.check(ffsubsync_output(offset=audio_check.REFERENCE_OFFSET_MAX))[0].ok)

    def test_the_scale_limit_itself_is_allowed(self):
        with mock.patch.object(audio_check, "REFERENCE_SCALE_TOLERANCE", 0.25):
            self.assertTrue(self.check(ffsubsync_output(scale=1.25))[0].ok)
            self.assertFalse(self.check(ffsubsync_output(scale=1.25 + 2 ** -10))[0].ok)

    def test_a_score_at_or_below_the_floor_means_the_reference_does_not_match_the_audio(self):
        for score in (-3.0, audio_check.MIN_SCORE):
            result, _ = self.check(ffsubsync_output(score=score, ok=False))
            self.assertEqual((result.ok, result.lasting), (False, True), score)
            self.assertIn("does not match the audio", result.why)
        self.assertTrue(self.check(ffsubsync_output(score=0.5))[0].ok)

    def test_the_last_result_and_the_last_score_count(self):
        reply = completed(stdout=result_line(offset=-9.0) + "\n" + result_line(offset=0.5),
                          stderr="score: -5.000\nscore: 12.000\n")
        result, _ = self.check(reply)
        self.assertEqual((result.ok, result.why), (True, "reference offset +0.50s scale 1.000"))

    def test_failures_are_worth_trying_again(self):
        for reply in (ffsubsync_output(code=1), completed(stdout="nothing useful"), ffsubsync_output(ok=False),
                      completed(stdout="RESULT {broken", stderr="score: 5"), ffsubsync_output(offset=True),
                      ffsubsync_output(scale=None)):
            result, _ = self.check(reply)
            self.assertEqual((result.ok, result.why, result.lasting), (False, "ffsubsync failed", False))

    def test_an_interpreter_without_ffsubsync_says_how_to_fix_it(self):
        result, _ = self.check(completed(stdout='RESULT {"importable": false}', code=1))
        self.assertEqual((result.ok, result.why, result.lasting), (False, audio_check.NOT_IMPORTABLE, False))

    def test_a_timeout_or_a_missing_interpreter_is_a_clear_message_worth_trying_again(self):
        result, _ = self.check(error=subprocess.TimeoutExpired("ffsubsync", 900))
        self.assertEqual((result.ok, result.lasting, result.detail), (False, False, "r.srt"))
        self.assertIn("took longer than 900 s", result.why)
        result, _ = self.check(error=FileNotFoundError())
        self.assertEqual((result.why, result.lasting, result.detail),
                         ("the ffsubsync interpreter was not found", False, "py.exe"))

    def test_without_an_interpreter_the_check_is_off(self):
        result, run = self.check(python="")
        self.assertEqual((result.ok, result.lasting), (False, True))
        self.assertIn("not configured", result.why)
        run.assert_not_called()


class CheckScript(TempFolder):
    """ffsubsync_check.py against a stand-in ffsubsync package."""

    def setUp(self):
        super().setUp()
        for name in ("ffsubsync", "ffsubsync.ffsubsync"):
            self.addCleanup(sys.modules.pop, name, None)
        self.addCleanup(setattr, sys, "path", sys.path[:])
        self.addCleanup(setattr, sys, "argv", sys.argv[:])

    def run_check(self, folders):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = ffsubsync_check.main([json.dumps(folders), "v.mkv", "r.srt", "o.srt", "60"])
        return code, out.getvalue()

    def test_it_runs_ffsubsync_with_the_arguments_and_prints_its_result(self):
        package = self.tmp / "ffsubsync"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "ffsubsync.py").write_text(textwrap.dedent("""
            import argparse, sys
            def make_parser():
                return argparse.ArgumentParser()
            def run(parser):
                print("score: 12.000", file=sys.stderr)
                return {"retval": 0, "sync_was_successful": True, "offset_seconds": 0.5,
                        "framerate_scale_factor": 1.0, "args": sys.argv[1:]}
            """))
        code, output = self.run_check([str(self.tmp)])
        self.assertEqual(code, 0)
        result = audio_check.last_result(output)
        self.assertEqual((result["offset_seconds"], result["sync_was_successful"]), (0.5, True))
        self.assertNotIn("args", result)

    def test_without_ffsubsync_it_says_so(self):
        with mock.patch.dict(sys.modules, {"ffsubsync": None}):
            code, output = self.run_check([str(self.tmp)])
        self.assertEqual((code, audio_check.last_result(output)), (1, {"importable": False}))

    def test_wrong_arguments_print_the_usage(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(ffsubsync_check.main(["only one"]), 2)
        self.assertIn("usage: ffsubsync_check.py", err.getvalue())
