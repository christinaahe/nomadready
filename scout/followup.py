"""Fast path for questions about the answer already given ("which of those has the fastest internet?").

The intake step flags a follow-up that asks about the previous report without changing the trip.
This answers it from that report alone, in seconds, citing the report's own citation ids, instead of
rerunning minutes of research. A code check makes sure every number in the answer appears in the
report (one rewrite if not). If the report doesn't hold what's needed, the agent says so and the
pipeline falls back to the full research.
"""

import asyncio
import re

from agents import Agent, Runner
from pydantic import BaseModel, Field

from scout.config import MODEL, SETTINGS
from scout.state import Research
from scout.verify import _numbers, _prose
from scout.writer import cited_ids, sources_section

FOLLOWUP_TIMEOUT_S = 60
LIMITS_RE = re.compile(r"^## What this data can.?t tell you", re.M | re.I)
SOURCE_LINE_RE = re.compile(r"^- \*\*\[([^\]]+)\]\*\* (.+?): (.*?)(?: <(https?://[^>]+)>)?\s*$")


class FollowupAnswer(BaseModel):
    needs_new_research: bool = Field(
        description="True if the report doesn't contain what's needed and answering needs new data "
                    "(a city or neighbourhood it didn't cover, a search it didn't run).")
    answer: str = Field(description="The answer in markdown, with citations. Empty if needs_new_research.")


followup_agent = Agent(
    name="Lead",
    instructions="""\
Answer the user's follow-up question about a report NomadReady already gave them, using ONLY that
report (and the conversation since). Be short: one to five sentences or a small table.

- Every number must appear in the report, copied exactly (rounding to whole numbers is fine), with the
  same citation ids in square brackets that the report puts after it, e.g. "271 Mbps [ookla-fixed-2026-Q3]".
  Use only citation ids that appear in the report.
- Comparing or ranking what's in the report is fine ("Lisbon's 406 Mbps is the fastest of the three").
- If the report says the data can't answer something (visas, safety, nightlife, live prices), say that
  plainly; don't answer from your own knowledge.
- If answering needs data the report doesn't have (another city, a neighbourhood it didn't list,
  coworking it didn't search), set needs_new_research and leave the answer empty.
- Speak as NomadReady to the user ("my data doesn't cover visas", "in the comparison above…"), not
  about "the provided report".
- Don't add a Sources section: it's added for you.""",
    model=MODEL,
    model_settings=SETTINGS,
    output_type=FollowupAnswer,
)


def last_report(conversation: list[dict]) -> int | None:
    """Index of the most recent full report (it has a limits section and Sources), not a quick answer."""
    for i in range(len(conversation) - 1, -1, -1):
        m = conversation[i]
        if m["role"] == "assistant" and LIMITS_RE.search(m["content"]) and "\n## Sources" in m["content"]:
            return i
    return None


def report_sources(report: str) -> dict[str, dict]:
    """The report's Sources list back into citations: id -> {source, detail, url}."""
    out = {}
    for line in report.split("\n## Sources", 1)[-1].splitlines():
        m = SOURCE_LINE_RE.match(line.strip())
        if m:
            out[m.group(1)] = {"source": m.group(2), "detail": m.group(3), "url": m.group(4)}
    return out


def unsupported_numbers(answer: str, report: str) -> list[float]:
    known = set()
    for v in _numbers(_prose(report)):
        known |= {v, round(v), round(v, 1)}
    return sorted({n for n in _numbers(_prose(answer)) if n not in known and round(n) not in known})


async def answer_from_report(conversation: list[dict], state: Research) -> str | None:
    """The answer (with its Sources), or None to fall back to full research."""
    at = last_report(conversation[:-1])
    if at is None:
        return None
    report = conversation[at]["content"]
    citations = report_sources(report)
    since = conversation[at + 1:-1]
    prompt = (f"The report:\n{report}\n\n"
              + ("Conversation since the report:\n" + "\n".join(f"{m['role'].upper()}: {m['content']}" for m in since)
                 + "\n\n" if since else "")
              + f"Follow-up question: {conversation[-1]['content']}")

    async def run(text: str) -> FollowupAnswer:
        result = await asyncio.wait_for(Runner.run(followup_agent, text, context=state, hooks=state.hooks, max_turns=1),
                                        timeout=FOLLOWUP_TIMEOUT_S)
        return result.final_output

    try:
        out = await run(prompt)
        if out.needs_new_research or not out.answer.strip():
            return None
        answer = out.answer.strip()
        problems = []
        if bad := unsupported_numbers(answer, report):
            problems.append(f"These numbers aren't in the report: {', '.join(f'{n:g}' for n in bad)}. "
                            "Use only the report's numbers, or leave them out.")
        if unknown := [c for c in cited_ids(answer) if c not in citations]:
            problems.append(f"These citation ids aren't in the report: {', '.join(unknown)}.")
        state.hooks.send({"type": "checked", "problems": [{"sentence": p, "verdict": "unsupported",
                                                           "explanation": ""} for p in problems],
                          "unknown_citations": unknown})
        if problems:
            out = await run(f"{prompt}\n\nYour answer:\n{answer}\n\nProblems:\n- " + "\n- ".join(problems)
                            + "\n\nReturn the corrected answer.")
            if out.needs_new_research or not out.answer.strip():
                return None
            answer = out.answer.strip()
    except Exception:  # timeouts, model errors: the full research still works
        return None

    for cid in cited_ids(answer):
        if cid not in citations:
            answer = answer.replace(f"[{cid}]", "[unverified]")
    state.citations.update(citations)
    used = [c for c in cited_ids(answer) if c in citations]
    return answer + ("\n" + sources_section(used, citations) if used else "")
