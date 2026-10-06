"""Collect semantic-layer tests without opening an external graph connection."""
from pathlib import Path
import sys

CANDIDATE_ROOT = Path(__file__).resolve().parents[2]
if str(CANDIDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_ROOT))
