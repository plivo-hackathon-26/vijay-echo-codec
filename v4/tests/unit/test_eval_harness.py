"""Phase 5a — measurement harness, deterministic mode (no live LLM).

Reproducible: drives the real firewall path with a perfect oracle verifier
and asserts the structural invariants. Exact numbers live in the reported
scorecard; here we lock the invariants that must always hold."""

from __future__ import annotations

from pathlib import Path

from plivo_mirror.eval import evaluate, load_cases

_V3 = Path(__file__).resolve().parents[3] / "v3" / "datasets"
_GOLD = Path(__file__).resolve().parents[2] / "datasets" / "golden_v1.jsonl"
_POLICIES = _V3 / "policies_v1.txt"
_INDUCED = _V3 / "eval_v1.jsonl"


def test_datasets_present_and_labeled():
    induced = load_cases(_INDUCED)
    golden = load_cases(_GOLD)
    # the induced file is a MIXED set: violations + clean near-misses
    violations = [c for c in induced if c.expected_intervene]
    assert len(violations) >= 10
    assert len(golden) >= 15 and all(not c.expected_intervene for c in golden)


async def test_deterministic_scorecard_invariants():
    sc = await evaluate(
        induced_path=str(_INDUCED),
        golden_path=str(_GOLD),
        policies_path=str(_POLICIES),
        mode="deterministic",
    )
    assert sc["mode"] == "deterministic"
    assert "ORACLE" in sc["model"]

    ind = sc["induced"]
    gold = sc["golden"]
    # honest labels are present
    assert "INDUCED" in ind["label"] and "organic" in ind["label"]
    assert "GOLDEN" in gold["label"]

    # rates are valid fractions
    assert 0.0 <= ind["catch_rate"] <= 1.0
    assert 0.0 <= gold["false_intervention_rate"] <= 1.0

    # ORACLE INVARIANT: a perfect verifier never says "supported" for an
    # induced violation, so nothing is missed AT the verifier in det mode —
    # all misses are at the gate (the lexicon ceiling).
    assert ind["missed_at_verifier"] == 0
    assert ind["caught"] + ind["missed_at_gate"] == ind["n"]

    # latency must be ABSENT in deterministic mode (not fabricated)
    assert "ABSENT" in sc["latency"]


async def test_clean_nospan_golden_cases_never_fire_deterministic():
    # no-risk-span clean cases must pass at the gate (verifier never reached)
    sc = await evaluate(
        induced_path=str(_INDUCED),
        golden_path=str(_GOLD),
        policies_path=str(_POLICIES),
        mode="deterministic",
    )
    # under the oracle, golden FPs can only come from deterministic/action
    # layers; clean_nospan cases carry no span and no tool call → 0 FPs there
    by_cat = sc["golden"]["by_category"]
    assert by_cat.get("clean_nospan", {}).get("fired", 0) == 0
