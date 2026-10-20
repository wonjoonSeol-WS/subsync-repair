"""Run by the interpreter that has ffsubsync, which may be another Python: ffsubsync_check.py <import folders as JSON>
<video> <reference.srt> <output.srt> <max offset seconds>. It prints ffsubsync's log, then "RESULT " and JSON."""
import json
import sys

RESULT_PREFIX = "RESULT "
RESULT_KEYS = ("retval", "sync_was_successful", "offset_seconds", "framerate_scale_factor")
USAGE = "usage: ffsubsync_check.py <import folders as JSON> <video> <reference.srt> <output.srt> <max offset seconds>"


def main(argv):
    if len(argv) != 5:
        print(USAGE, file=sys.stderr)
        return 2
    folders, video, reference, output, max_offset = argv
    sys.path[:0] = json.loads(folders)
    try:
        from ffsubsync.ffsubsync import make_parser, run
    except ModuleNotFoundError as e:
        print(f"cannot import ffsubsync: {e}", file=sys.stderr)
        print(RESULT_PREFIX + json.dumps({"importable": False}), flush=True)
        return 1
    sys.argv = ["ffsubsync", video, "-i", reference, "-o", output, "--max-offset-seconds", max_offset, "--gss"]
    result = run(make_parser())
    print(RESULT_PREFIX + json.dumps({key: result.get(key) for key in RESULT_KEYS}), flush=True)
    return result.get("retval") or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
