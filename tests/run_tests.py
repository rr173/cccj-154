"""Run the test suite with the standard library (no third-party packages).

Usage:
    python3 -I -m unittest discover -s tests -t .      # may not resolve -I
    python3 tests/run_tests.py                        # always works
"""

from __future__ import annotations

import os
import sys
import unittest


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    for p in (root, here):
        if p not in sys.path:
            sys.path.insert(0, p)
    loader = unittest.TestLoader()
    suite = loader.discover(here, pattern="test_*.py", top_level_dir=here)
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
