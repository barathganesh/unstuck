import asyncio
from threading import Event

import pytest
from test_controller import FakeDecider, blocked_by, replace, setup

from unstuck.domain import Reason, Settings
from unstuck.fixtures import load_fixture


@pytest.mark.parametrize("method", ["report", "diagnose", "recheck"])
def test_TST_18_blocking_tools_stop_before_return(tmp_path, monkeypatch, method):
    fixture = load_fixture()
    decider = FakeDecider()
    if method == "recheck":
        fixture = replace(fixture, job_state="pending")
        decider = FakeDecider(choice="inspect_pipeline")
    controller, repo, run, _ = setup(tmp_path, fixture, decider)
    controller.settings = Settings(provider_seconds=0.05)
    release = Event()
    returned = Event()
    original = getattr(controller.engine, method)

    def blocked(*args):
        release.wait(2)
        try:
            return original(*args)
        finally:
            returned.set()

    monkeypatch.setattr(controller.engine, method, blocked)
    try:
        result = asyncio.run(controller.run(run.state.run_id))
        assert not returned.is_set(), "deadline waited for blocked tool completion"
        if method == "diagnose":
            blocked_by(result, Reason.TOOL_ERROR.value)
        else:
            assert result["reason_code"] == Reason.TOOL_ERROR.value
        before = repo.load_run(run.state.run_id)
        assert [event.type for event in before.events].count("run_finished") == 1
    finally:
        release.set()
        assert returned.wait(2)
    assert repo.load_run(run.state.run_id) == before
    assert before.incident.counters.history_lookups == 0
    controller.engine.close()
