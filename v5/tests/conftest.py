"""Make ``plivo_mirror_v5`` importable when running tests straight from
the repo (without ``pip install -e v5``)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
