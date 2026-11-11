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


# --- regional fallback: a 429 on Vertex's `global` endpoint is retried on `us` ---


def test_rate_limited_call_moves_to_the_fallback_location(monkeypatch):
    import asyncio

    import litellm
    from agents import ModelSettings
    from agents.extensions.models.litellm_model import LitellmModel

    from scout.config import FALLBACK_LOCATION, MODEL, SETTINGS
    seen = []

    async def fake_get_response(self, system_instructions, input, model_settings, *args, **kwargs):
        loc = model_settings.extra_args["vertex_location"]
        seen.append(loc)
        if loc == "global":
            raise litellm.RateLimitError(message="Resource exhausted", llm_provider="vertex_ai", model="gemini")
        return f"answered on {loc}"
    monkeypatch.setattr(LitellmModel, "get_response", fake_get_response)
    out = asyncio.run(MODEL.get_response(None, "hi", SETTINGS, [], None, [], None))
    assert out == f"answered on {FALLBACK_LOCATION}" and seen == ["global", FALLBACK_LOCATION]
    # Other errors (a bad request, say) aren't retried elsewhere
    seen.clear()

    async def bad_request(self, system_instructions, input, model_settings, *args, **kwargs):
        seen.append(model_settings.extra_args["vertex_location"])
        raise litellm.BadRequestError(message="bad", llm_provider="vertex_ai", model="gemini")
    monkeypatch.setattr(LitellmModel, "get_response", bad_request)
    with pytest.raises(litellm.BadRequestError):
        asyncio.run(MODEL.get_response(None, "hi", ModelSettings(extra_args={"vertex_location": "global"}), [], None, [], None))
    assert seen == ["global"]


# --- wall-clock limits: a stalled model call degrades the answer instead of hanging it ---


def _findings(city):
    from scout.researcher import CityFindings
    return CityFindings(city=city, fit="good", summary=f"{city} summary.", n_available=1, n_within_budget=1,
                        median_total_usd=1, work_hours_local=None, hours_outside_8am_10pm=None, weather=None,
                        advisory_level=None, advisory_url=None, neighbourhoods=[], facts=[], gaps=["x"])


def _stalls(seconds=5, before=None):
    import asyncio

    async def run(agent, input, context=None, **kw):
        if before:
            before(context)
        await asyncio.sleep(seconds)
    return run


def test_stalled_researcher_becomes_a_gap(monkeypatch):
    import asyncio

    from agents.tool_context import ToolContext

    import scout.lead as lead
    monkeypatch.setattr(lead, "RESEARCHER_TIMEOUT_S", 0.2)
    monkeypatch.setattr(lead.Runner, "run", _stalls())
    s = Research(hooks=RecordingHooks())
    s.brief = {"cities": ["Porto"]}
    args = json.dumps({"city": "Porto", "task": "Full first look."})
    out = json.loads(asyncio.run(lead.research_city.on_invoke_tool(
        ToolContext(context=s, tool_name="research_city", tool_call_id="t", tool_arguments=args), args)))
    assert "timed out" in out["error"] and not s.findings


def test_research_phase_limit_still_writes_from_what_finished(monkeypatch):
    import asyncio

    import scout.pipeline as pipeline
    brief = {"cities": ["Lisbon", "Porto"]}

    async def fake_intake(conversation, state, hooks):
        return brief, None
    notes = {}

    async def fake_write(state, lead_notes):
        notes["lead"] = lead_notes
        return "report"
    monkeypatch.setattr(pipeline, "intake", fake_intake)
    monkeypatch.setattr(pipeline, "write_report", fake_write)
    monkeypatch.setattr(pipeline, "RESEARCH_PHASE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(pipeline.Runner, "run", _stalls(before=lambda st: st.findings.update({"Researcher-Lisbon": _findings("Lisbon")})))
    response, _ = asyncio.run(pipeline.answer([{"role": "user", "content": "x"}]))
    assert response == "report" and "Not researched in time: Porto" in notes["lead"]


def test_stalled_writer_sends_the_findings_instead(monkeypatch):
    import asyncio

    import scout.writer as writer
    monkeypatch.setattr(writer, "MIN_WRITE_S", 0.2)
    monkeypatch.setattr(writer, "WRITER_DEADLINE_S", 0)
    monkeypatch.setattr(writer.Runner, "run", _stalls())
    s = Research(hooks=RecordingHooks())
    s.brief = {"cities": ["Lisbon"]}
    s.findings["Researcher-Lisbon"] = _findings("Lisbon")
    report = asyncio.run(writer.write_report(s, "notes"))
    assert "took too long" in report and "Lisbon summary." in report


def test_a_broken_listener_does_not_break_the_run():
    def bad(event):
        raise ValueError("listener gone")
    RecordingHooks(bad).phase("intake", "x")  # must not raise
