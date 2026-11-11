"""One question's shared state, and the hooks that record every tool call with its agent."""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from agents import RunHooks

from scout.progress import describe_end, describe_start

log = logging.getLogger("nomadready")


@dataclass
class Research:
    """Shared by every agent and tool in one question's run. The models never see it."""

    hooks: Any = None  # the RecordingHooks, so sub-agent runs record into the same tool_calls
    brief: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)  # /chat's tool_calls, in order
    citations: dict[str, dict] = field(default_factory=dict)  # citation id -> {source, detail, url}
    findings: dict[str, Any] = field(default_factory=dict)  # researcher name -> CityFindings
    researchers: int = 0
    researcher_runs: dict[str, int] = field(default_factory=dict)  # city -> runs, to name follow-ups
    lead_turn: int = 0
    research_rounds: set[int] = field(default_factory=set)  # Lead turns that spawned researchers
    coworking_calls: int = 0
    queries_by_agent: dict[str, int] = field(default_factory=dict)  # query_listings calls per researcher
    corrections: list[str] = field(default_factory=list)  # researcher numbers fixed from tool results
    started: float = field(default_factory=time.monotonic)

    def elapsed(self) -> float:
        return time.monotonic() - self.started


def _json(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


class RecordingHooks(RunHooks):
    """Records each tool call as {agent, name, args, result}: the Project 1 /chat shape plus
    `agent`. Also collects the citations tool results carry, so the Writer's [ids] can be checked
    and the Sources list built from what the tools actually returned. emit (optional) gets progress
    events for the streaming /run page:
        {"type": "phase", "phase", "text"}                  intake / research / writing / checking / revising
        {"type": "agent", "agent", "status"}                started / finished
        {"type": "step", "status", "id", "agent", "tool", "text"}   a tool call starting, then what it found
        {"type": "tool_call", "agent", "name", "args", "result"}    the full record, as /chat returns it
    Every event also carries "t": seconds since the question started."""

    def __init__(self, emit: Callable[[dict], None] | None = None):
        self._emit = emit
        self.started = time.monotonic()

    def send(self, event: dict) -> None:
        """Send a progress event (with seconds since the start) to the page, if one is listening.
        A broken listener must never break the answer. Progress is also logged, so a stalled run
        shows where it stopped (Cloud Run keeps stdout in Cloud Logging)."""
        event = {**event, "t": round(time.monotonic() - self.started, 1)}
        kind = event.get("type")
        if kind in ("phase", "agent") or (kind == "step" and event.get("status") == "end"):
            log.info("%6.1fs %-6s %s %s", event["t"], kind, event.get("agent", ""),
                     event.get("text") or event.get("status", ""))
        if self._emit:
            try:
                self._emit(event)
            except Exception:
                pass

    def phase(self, phase: str, text: str) -> None:
        self.send({"type": "phase", "phase": phase, "text": text})

    async def on_agent_start(self, context, agent):
        self.send({"type": "agent", "agent": agent.name, "status": "started"})

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        if agent.name == "Lead":
            context.context.lead_turn += 1

    async def on_tool_start(self, context, agent, tool):
        args = _json(getattr(context, "tool_arguments", "{}"))
        self.send({"type": "step", "status": "start", "id": getattr(context, "tool_call_id", None),
                   "agent": agent.name, "tool": tool.name, "text": describe_start(tool.name, args)})

    async def on_tool_end(self, context, agent, tool, result):
        state: Research = context.context
        result_text = result if isinstance(result, str) else json.dumps(result, default=str)
        call = {
            "agent": agent.name,
            "name": tool.name,
            "args": _json(getattr(context, "tool_arguments", "{}")),
            "result": result_text,
        }
        state.tool_calls.append(call)
        parsed = _json(result_text)
        if isinstance(parsed, dict):
            for c in parsed.get("citations", []):
                if isinstance(c, dict) and c.get("id"):
                    state.citations.setdefault(c["id"], c)
        self.send({"type": "step", "status": "end", "id": getattr(context, "tool_call_id", None),
                   "agent": agent.name, "tool": tool.name, "text": describe_end(tool.name, call["args"], result_text)})
        self.send({"type": "tool_call", "id": getattr(context, "tool_call_id", None), **call})

    async def on_agent_end(self, context, agent, output):
        self.send({"type": "agent", "agent": agent.name, "status": "finished"})
