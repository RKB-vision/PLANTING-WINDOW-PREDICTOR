"""Automated tests for the plant-readiness prototype.

Run from the project root:

    python -m unittest discover -s tests -v
"""

import sys
from pathlib import Path

# Make the project root importable when running unittest from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
