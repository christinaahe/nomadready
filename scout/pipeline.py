"""One question, start to finish: intake (clarify or brief) -> Lead fan-out and gap loop -> Writer."""

import asyncio
import json
from typing import Callable

from agents import Runner
from agents.exceptions import MaxTurnsExceeded

from scout.config import LEAD_MAX_TURNS, RESEARCH_PHASE_TIMEOUT_S
from scout.followup import answer_from_report
from scout.intake import intake
from scout.lead import lead
from scout.state import RecordingHooks, Research
from scout.writer import write_report


async def answer(conversation: list[dict], emit: Callable[[dict], None] | None = None) -> tuple[str, Research]:
    """conversation: the session's messages ({role, content}), ending with the new user message.
    Returns the response text (a clarifying question or the report) and the run's state."""
    hooks = RecordingHooks(emit)
    state = Research(hooks=hooks)

    # 1. Clarify or brief
    hooks.phase("intake", "Reading your request")
    brief, question = await intake(conversation, state, hooks)
    if question:
        return question, state

    # 1b. A question about the answer already given: answer from that report, in seconds
    if brief.pop("about_previous_answer", False):
        hooks.phase("writing", "Answering from the previous report")
        quick = await answer_from_report(conversation, state)
        if quick:
            state.brief, state.followup = brief, True
            return quick, state
        hooks.phase("intake", "The previous report doesn't cover that, so researching it")

    state.brief = brief
    hooks.send({"type": "brief", "brief": brief})

    # 2. Research: the Lead fans out researchers, checks for gaps, follows up
    hooks.phase("research", f"Researching {', '.join(brief['cities'])}" if brief["cities"]
                else "Picking cities to compare")
    try:
        result = await asyncio.wait_for(
            Runner.run(lead, f"Trip brief:\n{json.dumps(brief, indent=1)}", context=state,
                       hooks=hooks, max_turns=LEAD_MAX_TURNS),
            timeout=RESEARCH_PHASE_TIMEOUT_S)
        lead_notes = str(result.final_output)
    except MaxTurnsExceeded:
        lead_notes = "(The lead ran out of turns; rank from the findings.)"
    except TimeoutError:
        # Researchers still running are cancelled; what finished is in state.findings
        missing = [c for c in brief["cities"] if not any(f.city == c for f in state.findings.values())]
        lead_notes = (f"(Research stopped at the {RESEARCH_PHASE_TIMEOUT_S} s time limit. Rank from the findings. "
                      + (f"Not researched in time: {', '.join(missing)}; say so plainly.)" if missing else ")"))
        hooks.send({"type": "agent", "agent": "Lead", "status": "finished"})
    if not state.findings:
        return ("I couldn't complete the research for this question (no researcher returned findings). "
                "Please try again in a moment."), state

    # 3. Write, outside the loop: the Writer sees only the brief, notes and findings
    return await write_report(state, lead_notes), state
