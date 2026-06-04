from plivo_mirror_v5.engine import ReferenceStore


def test_flattens_nested_keys():
    store = ReferenceStore({"plan": {"turbo": {"price_per_month": 79.99}}})
    assert store.get("plan.turbo.price_per_month") == 79.99
    assert store.keys() == ["plan.turbo.price_per_month"]


def test_lookup_distinguishes_absent_from_falsy():
    store = ReferenceStore({"policy": {"cancellation_fee": 0}})
    value, found = store.lookup("policy.cancellation_fee")
    assert (value, found) == (0, True)
    value, found = store.lookup("policy.refund_window_days")
    assert (value, found) == (None, False)


def test_no_fuzzy_matching():
    store = ReferenceStore({"hours": {"weekend": "9am-5pm"}})
    assert store.get("hours.weekends") is None  # exact keys only, by design
    assert not store.has("hours")               # intermediate nodes not addressable
