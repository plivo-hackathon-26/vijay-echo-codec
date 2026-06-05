#!/usr/bin/env python
"""Export the reviewer-labeled flags as a JSONL training set.

Phase 1 of the roadmap: the review loop (✓ real / ✗ false alarm) already
labels every flag on real traffic. This turns those labels into the seed
corpus for a dedicated guard model — each row is the agent turn, its
context, the verdict evidence, and the human label.

    venv/bin/python v5/plivo_mirror_v5/deployables/monitoring/export_dataset.py \
        --db v5/mirror.db --out v5/datasets/guard_seed.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="v5/mirror.db")
    parser.add_argument("--out", default="v5/datasets/guard_seed.jsonl")
    args = parser.parse_args()

    rows = CallStore(args.db).export_labeled_dataset()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))

    confirmed = sum(1 for r in rows if r["label"] == "confirmed")
    rejected = len(rows) - confirmed
    print(f"wrote {len(rows)} labeled rows → {out}")
    print(f"  {confirmed} confirmed (real violations) · {rejected} rejected "
          f"(false alarms)")
    if not rows:
        print("  (no reviewer labels yet — review flags in the dashboard first)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
