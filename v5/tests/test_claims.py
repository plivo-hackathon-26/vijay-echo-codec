from plivo_mirror_v5.engine.claims import LexiconClaimExtractor
from plivo_mirror_v5.integrations import ConversationItem

from helpers import REFERENCE

EXTRACTOR = LexiconClaimExtractor(
    REFERENCE,
    action_verbs={"cancel_service": ["cancelled", "canceled"],
                  "schedule_visit": ["scheduled", "booked"]},
)


def claims_for(text):
    return EXTRACTOR.extract_from_text(text)


def test_price_claim_from_reference_lexicon():
    [c] = claims_for("Great question! The Turbo plan is $59.99 a month.")
    assert c["claim_type"] == "price"
    assert c["ref"] == "reference.plan.turbo.price_per_month"
    assert c["spoken_value"] == "59.99"


def test_distinguishes_plans():
    [c] = claims_for("The basic plan costs 49.99 dollars monthly.")
    assert c["ref"] == "reference.plan.basic.price_per_month"


def test_hours_claim_with_plural_trigger():
    [c] = claims_for("We're available 9am-5pm on weekends.")
    assert c["claim_type"] == "hours"
    assert c["ref"] == "reference.hours.weekend"
    assert c["spoken_value"] == "9am-5pm"


def test_policy_claim_requires_unit():
    [c] = claims_for("You can get a full refund within 60 days.")
    assert c["claim_type"] == "policy"
    assert c["ref"] == "reference.policy.refund_window_days"
    assert c["spoken_value"] == "60"
    # "refund" without the days unit must NOT produce a policy claim
    assert claims_for("I'll process your refund of $40 right away.") == []


def test_action_claim_from_verb_map():
    [c] = claims_for("Done — I've cancelled your service effective today.")
    assert c["claim_type"] == "action"
    assert c["ref"] == "tool.cancel_service"


def test_no_trigger_no_claim():
    assert claims_for("Thanks for calling, have a great day!") == []
    # number without any key triggers
    assert claims_for("You are caller number 7 in the queue.") == []


def test_extract_keeps_attached_claims_and_dedupes_by_ref():
    attached = [{"claim_id": "h1", "claim_type": "price", "spoken_value": "59.99",
                 "ref": "reference.plan.turbo.price_per_month"}]
    item = ConversationItem(role="agent",
                            text="The Turbo plan is $59.99 a month.",
                            claims=attached)
    out = EXTRACTOR.extract(item)
    assert out == attached  # host claim wins; lexicon duplicate dropped


def test_user_turns_not_extracted():
    item = ConversationItem(role="user", text="Is the turbo plan $59.99?")
    assert EXTRACTOR.extract(item) == []
