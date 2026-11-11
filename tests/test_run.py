"""Offline tests for /run's event stream (the pipeline is stubbed: no model calls) and the
readable progress lines."""

import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
from scout.progress import describe_end, describe_start
from scout.state import RecordingHooks, Research


def parse(body: str) -> list[dict]:
    return [json.loads(line[6:]) | {"_event": block.split("\n")[0][7:]}
            for block in body.strip().split("\n\n") if block.startswith("event: ")
            for line in block.split("\n") if line.startswith("data: ")]


@pytest.fixture
def client(monkeypatch):
    async def fake_answer(conversation, emit=None):
        hooks = RecordingHooks(emit)
        state = Research(hooks=hooks)
        hooks.phase("intake", "Reading your request")
        state.brief = {"cities": ["Lisbon"]}
        hooks.send({"type": "tool_call", "agent": "Researcher-Lisbon", "name": "find_stays", "args": {}, "result": "{}"})
        state.tool_calls.append({})
        return f"report for: {conversation[-1]['content']}", state

    monkeypatch.setattr(app_module, "answer", fake_answer)
    app_module.sessions.clear()
    app_module.busy.clear()
    return TestClient(app_module.app)


def test_run_streams_session_progress_then_done(client):
    r = client.post("/run", json={"message": "Lisbon in March for a month"})
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse(r.text)
    assert [e["type"] for e in events] == ["session", "phase", "tool_call", "done"]
    assert all(e["_event"] == e["type"] for e in events)
    done = events[-1]
    assert done["response"] == "report for: Lisbon in March for a month" and done["clarifying"] is False
    assert done["n_tool_calls"] == 1 and "t" in events[1]
    sid = events[0]["session_id"]
    assert app_module.sessions[sid][-1] == {"role": "assistant", "content": done["response"]}


def test_run_continues_a_session(client):
    sid = parse(client.post("/run", json={"message": "first"}).text)[0]["session_id"]
    client.post("/run", json={"message": "second", "session_id": sid})
    assert [m["content"] for m in app_module.sessions[sid]] == ["first", "report for: first", "second",
                                                                "report for: second"]


def test_busy_session_is_refused(client):
    app_module.busy.add("s1")
    events = parse(client.post("/run", json={"message": "x", "session_id": "s1"}).text)
    assert events[-1]["type"] == "error" and "still working" in events[-1]["message"]
    assert "s1" not in app_module.sessions


def test_failure_becomes_an_error_event(client, monkeypatch):
    async def broken(conversation, emit=None):
        raise RuntimeError("quota")
    monkeypatch.setattr(app_module, "answer", broken)
    events = parse(client.post("/run", json={"message": "x"}).text)
    assert events[-1]["type"] == "error" and "RuntimeError: quota" in events[-1]["message"]
    assert not app_module.busy


def test_progress_lines():
    assert describe_start("find_stays", {"city": "Lisbon", "check_in": "2027-03-01", "check_out": "2027-03-29",
                                         "max_total_usd": 2000}) == \
        "searching stays in Lisbon (2027-03-01 to 2027-03-29, up to $2,000)"
    result = json.dumps({"n_available": 1234, "n_within_budget": 456, "median_total_usd": 1890.0,
                         "filters": {"max_total_usd": 2000}})
    assert describe_end("find_stays", {}, result) == "1,234 stays open, 456 within budget, median $1,890 for the stay"
    assert describe_end("find_stays", {}, json.dumps({"error": "Unknown city"})) == "error: Unknown city"
    assert describe_end("anything", {}, "not json") == "done"


def test_custom_queries_are_capped_per_researcher():
    import asyncio
    from types import SimpleNamespace

    from agents.tool_context import ToolContext

    from scout.config import MAX_QUERIES_PER_RESEARCHER
    from scout.researcher import query_listings

    s = Research()
    args = json.dumps({"sql": "select count(*) n from listings where city = 'Porto'"})

    def call(agent):
        ctx = ToolContext(context=s, tool_name="query_listings", tool_call_id="x", tool_arguments=args,
                          agent=SimpleNamespace(name=agent))
        return json.loads(asyncio.run(query_listings.on_invoke_tool(ctx, args)))

    for _ in range(MAX_QUERIES_PER_RESEARCHER):
        assert "rows" in call("Researcher-Porto")
    assert "limit" in call("Researcher-Porto")["error"]
    assert "rows" in call("Researcher-Lisbon")  # another researcher has its own allowance


def test_a_broken_listener_does_not_break_the_run():
    def bad(event):
        raise ValueError("listener gone")
    RecordingHooks(bad).phase("intake", "x")  # must not raise
