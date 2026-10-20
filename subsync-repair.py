#!/usr/bin/env python3
"""subsync-repair: repairs subtitles whose timing drifts in pieces. Run it with --help, or `serve --help`."""
import sys

from subsync_repair.cli import main

if __name__ == "__main__":
    sys.exit(main())
