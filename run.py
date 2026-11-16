#!/usr/bin/env python3
"""Launcher: works under ``python3 -I`` because sys.path[0] is the script dir."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from unitpipe.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
