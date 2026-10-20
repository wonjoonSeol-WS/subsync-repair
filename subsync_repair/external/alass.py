"""alass, which aligns a subtitle to a reference."""
import subprocess
from pathlib import Path

from subsync_repair.external import process
from subsync_repair.external.programs import ToolError

DEFAULT_ALASS = "alass-cli"
ALASS_TIMEOUT = 600


class Alass:
    def __init__(self, exe=DEFAULT_ALASS, timeout=ALASS_TIMEOUT):
        self.exe, self.timeout = str(exe), timeout

    def align(self, reference, work, out):
        """Write `work` aligned to `reference` into `out`, or raise ToolError."""
        try:
            r = process.run([self.exe, reference, work, out], self.timeout)
        except subprocess.TimeoutExpired:
            raise ToolError(f"alass took longer than {self.timeout} s") from None
        except FileNotFoundError:
            raise ToolError("alass-cli was not found", f"tools.alass is {self.exe!r}") from None
        except OSError as e:
            raise ToolError("alass-cli could not be started", repr(e)) from None
        if r.returncode != 0 or not Path(out).exists():
            raise ToolError("alass failed", process.tail(r))
