"""ReportGenerator + ReportSink tests."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plivo_mirror.config import MirrorConfig
from plivo_mirror.context import HistoryTurn, ToolCallIntent, Verdict
from plivo_mirror.reports.generator import ReportGenerator
from plivo_mirror.reports.schema import FailureReport, ReportStatus
from plivo_mirror.reports.sinks.memory import InMemoryReportSink
from plivo_mirror.reports.sinks.sqlite import SQLiteReportSink
from tests.unit.conftest import FakeLLM


def _cfg(llm) -> MirrorConfig:
    return MirrorConfig(llm=llm, policies=["dummy policy"])


# ── generator ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generator_returns_none_when_no_intervention() -> None:
    llm = FakeLLM()
    gen = ReportGenerator(_cfg(llm))
    report = await gen.generate(
        call_uuid="abc",
        tenant_id=None,
        history=[HistoryTurn(role="customer", text="hi")],
        verdicts=[Verdict.no_intervention("ok")],
    )
    assert report is None
    assert llm.calls == []


@pytest.mark.asyncio
async def test_generator_parses_llm_response() -> None:
    canned = {
        "pattern_name": "retracted_item",
        "severity": "high",
        "summary": "Agent placed both items.",
        "root_cause": "Prompt says capture every item.",
        "proposed_fix_text": "Replace the capture rule with latest-preference.",
        "proposed_file": "agent.py",
        "suggested_diff": "- capture every item\n+ use latest preference",
        "confidence": 0.92,
    }
    llm = FakeLLM(responder=lambda s, u: canned)
    gen = ReportGenerator(_cfg(llm))
    report = await gen.generate(
        call_uuid="abc-123",
        tenant_id=None,
        history=[HistoryTurn(role="customer", text="X, actually Y")],
        verdicts=[
            Verdict(score=0.95, reason="policy 3", should_intervene=True),
        ],
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
    )
    assert report is not None
    assert report.call_uuid == "abc-123"
    assert report.pattern_name == "retracted_item"
    assert report.severity == "high"
    assert report.confidence == pytest.approx(0.92)
    assert report.proposed_file == "agent.py"
    assert report.status == ReportStatus.PENDING


@pytest.mark.asyncio
async def test_generator_fails_open_on_llm_error() -> None:
    def boom(s: str, u: str | None) -> dict:
        raise RuntimeError("simulated")

    llm = FakeLLM(responder=boom)
    gen = ReportGenerator(_cfg(llm))
    report = await gen.generate(
        call_uuid="abc",
        tenant_id=None,
        history=[],
        verdicts=[Verdict(score=0.99, reason="bad", should_intervene=True)],
    )
    assert report is None


# ── in-memory sink ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inmemory_sink_create_get_list() -> None:
    sink = InMemoryReportSink()
    r1 = await sink.create(FailureReport(call_uuid="A", summary="first"))
    r2 = await sink.create(FailureReport(call_uuid="B", summary="second"))
    assert r1 == 1
    assert r2 == 2

    got = await sink.get(r1)
    assert got is not None
    assert got.summary == "first"

    rows = await sink.list()
    assert [r.id for r in rows] == [2, 1]  # newest first


@pytest.mark.asyncio
async def test_inmemory_sink_update_status() -> None:
    sink = InMemoryReportSink()
    rid = await sink.create(FailureReport(call_uuid="A"))
    await sink.update_status(
        rid, ReportStatus.APPLIED,
        applied_pr_url="https://github.com/x/y/pull/1",
        applied_at="2026-01-01T00:00:00Z",
    )
    got = await sink.get(rid)
    assert got.status == ReportStatus.APPLIED
    assert got.applied_pr_url == "https://github.com/x/y/pull/1"

    rows_pending = await sink.list(status=ReportStatus.PENDING)
    rows_applied = await sink.list(status=ReportStatus.APPLIED)
    assert rows_pending == []
    assert len(rows_applied) == 1


# ── sqlite sink ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sqlite_sink_persists_across_instances() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "test.db")
        sink1 = SQLiteReportSink(db_path=db)
        rid = await sink1.create(
            FailureReport(
                call_uuid="abc",
                pattern_name="retracted_item",
                severity="high",
                summary="test",
                confidence=0.8,
                extras={"a": 1, "b": "two"},
            )
        )
        assert rid >= 1

        # Re-open the DB from a fresh sink instance — durability check.
        sink2 = SQLiteReportSink(db_path=db)
        got = await sink2.get(rid)
        assert got is not None
        assert got.pattern_name == "retracted_item"
        assert got.severity == "high"
        assert got.confidence == pytest.approx(0.8)
        assert got.extras == {"a": 1, "b": "two"}


@pytest.mark.asyncio
async def test_sqlite_sink_status_filter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "test.db")
        sink = SQLiteReportSink(db_path=db)
        a = await sink.create(FailureReport(call_uuid="A"))
        b = await sink.create(FailureReport(call_uuid="B"))
        c = await sink.create(FailureReport(call_uuid="C"))
        await sink.update_status(b, ReportStatus.APPLIED, applied_pr_url="x")
        await sink.update_status(c, ReportStatus.DISMISSED)

        pending = await sink.list(status=ReportStatus.PENDING)
        applied = await sink.list(status=ReportStatus.APPLIED)
        dismissed = await sink.list(status=ReportStatus.DISMISSED)

        assert [r.call_uuid for r in pending] == ["A"]
        assert [r.call_uuid for r in applied] == ["B"]
        assert [r.call_uuid for r in dismissed] == ["C"]
