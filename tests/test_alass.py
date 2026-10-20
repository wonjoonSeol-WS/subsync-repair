"""Tests for subsync_repair/external/alass.py: the alass adapter, with a fake alass."""
import subprocess
from unittest import mock

from subsync_repair.external import process
from subsync_repair.external.alass import Alass
from subsync_repair.external.programs import ToolError
from support import TempFolder, completed


class AlassAdapter(TempFolder):
    def align(self, **kwargs):
        with mock.patch.object(process, "run", **kwargs) as run:
            Alass("/opt/alass/alass-cli").align(self.tmp / "r.srt", self.tmp / "w.srt", self.tmp / "o.srt")
        return run

    def test_a_missing_binary_a_timeout_and_a_failure_are_errors_whose_details_stay_out_of_the_message(self):
        failed = completed(stderr="bad input at C:\\x", code=1)
        for kwargs, message, detail in (({"side_effect": FileNotFoundError()}, "alass-cli was not found", "/opt/alass"),
                                        ({"side_effect": PermissionError()}, "could not be started", "PermissionError"),
                                        ({"side_effect": subprocess.TimeoutExpired("alass", 600)}, "took longer", ""),
                                        ({"return_value": failed}, "alass failed", "bad input at C:\\x")):
            with self.assertRaises(ToolError) as raised:
                self.align(**kwargs)
            self.assertIn(message, raised.exception.message)
            self.assertIn(detail, raised.exception.detail)
            self.assertNotIn("C:\\", raised.exception.message)

    def test_success_needs_the_output_file(self):
        with self.assertRaisesRegex(ToolError, "alass failed"):
            self.align(return_value=completed())
        (self.tmp / "o.srt").write_text("x")
        run = self.align(return_value=completed())
        self.assertEqual(run.call_args.args[0][0], "/opt/alass/alass-cli")
