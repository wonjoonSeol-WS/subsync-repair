"""Running the external tools: no console window, no standard input, and on a timeout the whole process tree ends."""
import contextlib
import os
import signal
import subprocess

OUTPUT_TAIL = 300           # characters of a tool's output kept for the log
KILL_WAIT = 5               # seconds to collect what a killed tool still writes
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd, timeout):
    """The finished `cmd` as a CompletedProcess with text output. Raises OSError when it cannot start, and
    subprocess.TimeoutExpired, after killing it and every process it started, when it runs too long."""
    with subprocess.Popen([str(part) for part in cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW, start_new_session=os.name != "nt") as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def kill_tree(proc):
    if os.name == "nt":
        with contextlib.suppress(OSError):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, creationflags=NO_WINDOW, check=False)
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=KILL_WAIT)


def tail(result):
    """The end of what a finished tool wrote, for the log."""
    return (result.stderr or result.stdout or "").strip()[-OUTPUT_TAIL:]
