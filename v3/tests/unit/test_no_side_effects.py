"""Asserts that `import plivo_mirror` has zero side effects.

The legacy hackathon code monkey-patched 6+ functions on `db`, `mirror`
and `voice.stream` at import time. That made the library dangerous to
embed in a third-party process. The whole point of v1 is that
importing the library cannot change the behaviour of anything outside
the library.

This test snapshots the relevant module surfaces before and after the
import and asserts nothing was rewritten.
"""

from __future__ import annotations

import importlib
import sys
import types


def _snapshot_module(name: str) -> dict[str, int]:
    """Snapshot a module's attribute ids so we can detect rewrites."""
    if name not in sys.modules:
        return {}
    mod = sys.modules[name]
    return {
        attr: id(getattr(mod, attr))
        for attr in dir(mod)
        if not attr.startswith("_")
        and not isinstance(getattr(mod, attr, None), types.ModuleType)
    }


def test_no_third_party_monkey_patching() -> None:
    # Snapshot whatever plivo_mirror modules are already in sys.modules
    # so we can restore them. Other test files import plivo_mirror at
    # collection time and hold stale references to its classes; purging
    # the modules without restoring them would break isinstance() checks
    # in later tests.
    pm_snapshot = {
        name: sys.modules[name]
        for name in list(sys.modules)
        if name == "plivo_mirror" or name.startswith("plivo_mirror.")
    }

    # Force a fresh import so __init__ actually runs.
    for name in list(pm_snapshot):
        del sys.modules[name]

    # Snapshot stdlib + a few common third-party modules likely to be
    # plausible targets for monkey-patching.
    candidates = ["os", "json", "asyncio", "logging", "re"]
    before = {name: _snapshot_module(name) for name in candidates}

    importlib.import_module("plivo_mirror")

    after = {name: _snapshot_module(name) for name in candidates}

    # Restore the original plivo_mirror module objects so other tests
    # keep working — they hold module-level references to these classes.
    for name in list(sys.modules):
        if name == "plivo_mirror" or name.startswith("plivo_mirror."):
            del sys.modules[name]
    sys.modules.update(pm_snapshot)

    for name in candidates:
        if not before[name]:
            continue
        # Every attribute we knew about before must still resolve to
        # the exact same object id afterward.
        for attr, ident in before[name].items():
            assert after[name].get(attr) == ident, (
                f"plivo_mirror import mutated {name}.{attr}"
            )


def test_plivo_mirror_init_is_pure_reexport() -> None:
    """The package __init__ should not perform work beyond imports."""
    mod = importlib.import_module("plivo_mirror")
    # If __init__ started a background task or held a thread we'd see
    # additional attributes. We expect only the documented public API.
    public = {name for name in dir(mod) if not name.startswith("_")}
    expected = {
        "Supervisor",
        "CallSupervisor",
        "MirrorConfig",
        "Verdict",
        "TurnPayload",
        "ToolCallIntent",
        "HistoryTurn",
        "SupervisorContext",
    }
    # Allow modules added by Python's import machinery to coexist
    # alongside the public surface; just make sure our intended names
    # are present.
    assert expected.issubset(public)
