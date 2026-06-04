#!/usr/bin/env python
"""Audit stored calls with the offline LLM judge (Layer 4).

    venv/bin/python -m plivo_mirror_v5.auditor.run_audit \
        --db v5/mirror_monitoring.db [--call-id <id>] \
        [--facts facts.json] [--policies policies.txt]

Prints judge findings: failures the inline layers missed, and inline flags
the judge disagrees with. Strictly offline/batch — never wire this inline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from plivo_mirror_v5.auditor.post_call_judge import LLMPostCallJudge
from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="v5/mirror_monitoring.db")
    parser.add_argument("--call-id", default=None, help="default: all calls")
    parser.add_argument("--facts", type=Path, default=None,
                        help="JSON object of reference facts to ground the judge")
    parser.add_argument("--policies", type=Path, default=None,
                        help="text file, one policy per line")
    args = parser.parse_args()

    facts = {}
    if args.facts:
        facts = {k: str(v) for k, v in json.loads(args.facts.read_text()).items()
                 if not k.startswith("_")}
    policies = []
    if args.policies:
        policies = [line.strip() for line in args.policies.read_text().splitlines()
                    if line.strip() and not line.startswith("#")]

    store = CallStore(args.db)
    judge = LLMPostCallJudge(facts=facts, policies=policies)
    call_ids = [args.call_id] if args.call_id else [
        c["call_id"] for c in store.list_calls()]

    total = 0
    for call_id in call_ids:
        call = store.get_call(call_id)
        if call is None:
            print(f"!! unknown call {call_id}", file=sys.stderr)
            continue
        findings = judge.audit_call(call)
        total += len(findings)
        print(f"{call_id}: {len(findings)} finding(s)")
        for f in findings:
            print(f"  [{f.kind}] {f.turn_id}: {f.rationale}")
    print(f"\n{total} finding(s) across {len(call_ids)} call(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
