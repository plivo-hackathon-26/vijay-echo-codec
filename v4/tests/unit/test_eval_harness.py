"""Phase 5a — measurement harness, deterministic mode (no live LLM).

Reproducible: drives the real firewall path with a perfect oracle verifier
and asserts the structural invariants. Exact numbers live in the reported
scorecard; here we lock the invariants that must always hold."""

from __future__ import annotations

from pathlib import Path

from plivo_mirror.eval import evaluate, load_cases, load_facts

_V3 = Path(__file__).resolve().parents[3] / "v3" / "datasets"
_DATASETS = Path(__file__).resolve().parents[2] / "datasets"
_GOLD = _DATASETS / "golden_v1.jsonl"
_FACTS = _DATASETS / "facts_v1.json"
_POLICIES = _V3 / "policies_v1.txt"
_INDUCED = _V3 / "eval_v1.jsonl"
_INDUCED_V2 = _V3 / "eval_v2.jsonl"  # the wider 100+-case set


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


def test_facts_v1_loads_and_skips_comment_keys():
    facts = load_facts(_FACTS)
    assert facts  # non-empty
    assert all(not k.startswith("_") for k in facts)  # _comment dropped
    assert "wings_per_order" in facts and "menu_items" in facts


async def test_deterministic_scorecard_records_facts_source():
    sc = await evaluate(
        induced_path=str(_INDUCED),
        golden_path=str(_GOLD),
        policies_path=str(_POLICIES),
        mode="deterministic",
        facts_path=str(_FACTS),
    )
    assert sc["facts_source"] == str(_FACTS)
    assert sc["facts_loaded"] >= 1
    # ORACLE short-circuits the verifier, so loading facts must NOT change the
    # structural invariants (facts only move LIVE-mode FPs). Lock that here so
    # the deterministic CI numbers stay honest about what facts do and don't do.
    assert sc["induced"]["missed_at_verifier"] == 0


async def test_eval_v2_wired_and_semantic_gate_gap_is_real():
    # CLAUDE.md: the regression suite wires to BOTH eval_v1 and eval_v2. This
    # locks the wider 65-violation ruler in CI (deterministic, no LLM) and
    # records the structural fact the NLI lever exists to fix: the lexicon
    # gate leaves the pure semantic-contradiction categories unrouted.
    induced = load_cases(_INDUCED_V2)
    violations = [c for c in induced if c.expected_intervene]
    assert len(violations) >= 60  # grown set, not the 16-case eval_v1

    sc = await evaluate(
        induced_path=str(_INDUCED_V2),
        golden_path=str(_GOLD),
        policies_path=str(_POLICIES),
        mode="deterministic",
    )
    ind = sc["induced"]
    # oracle invariant still holds on the wider set
    assert ind["missed_at_verifier"] == 0
    assert ind["caught"] + ind["missed_at_gate"] == ind["n"]
    # the semantic categories the gate cannot see (NLI targets) stay near-zero
    # fire under a PERFECT verifier — proving the gap is structural (the gate),
    # not the judge. If a future gate change starts routing these, this test
    # SHOULD be updated to reflect the new (higher) ceiling.
    by_cat = ind["by_category"]
    for cat in ("compound_modifier_dropped", "negation_ignored"):
        d = by_cat.get(cat, {})
        assert d.get("n", 0) >= 5  # the grown coverage is present
        assert d.get("fired", 0) == 0  # lexicon-invisible today


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
