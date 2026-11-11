"""Verifier: every number in the Writer's draft must trace back to a tool result.

Code finds the numbers that don't appear in any data-tool result of this run (after allowing for
rounding, percentages, and the brief's own numbers). The Verifier agent then judges each flagged
sentence against the raw tool results: correctly derived, wrong (with the right value), or
unsupported. If anything is wrong or unsupported, the Writer revises once.
"""

import json
import re
from typing import Literal

from agents import Agent, Runner
from pydantic import BaseModel

from scout.config import MODEL, SETTINGS
from scout.state import Research

DATA_TOOLS = {"find_stays", "neighbourhood_speed", "month_climate", "travel_advisory", "fx_to_usd",
              "work_overlap", "nearby_coworking", "query_listings"}
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[float]:
    out = set()
    for m in NUM_RE.findall(text):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _known(state: Research) -> set[float]:
    """Every number a data tool returned, plus forms a writer may legitimately use."""
    blob = " ".join(c["result"] for c in state.tool_calls if c["name"] in DATA_TOOLS)
    blob += " " + json.dumps(state.brief)
    known = set()
    for v in _numbers(blob):
        known |= {v, round(v), round(v, 1), round(v * 100), round(v * 100, 1)}  # 0.12 -> 12%
    return known


def _prose(report: str) -> str:
    """The report without citations, links and the Sources list, which are full of ids and dates."""
    report = report.split("\n## Sources")[0]
    report = re.sub(r"\]\([^)]*\)", "]", report)  # link targets
    return re.sub(r"\[[^\]]*-[^\]]*\]", "", report)  # [citation-ids]


def unsupported_sentences(report: str, state: Research) -> list[str]:
    """Sentences (or whole table rows, with their column headers) holding a number no tool returned,
    each prefixed with the section it's in, so the Verifier knows which city it's about."""
    known = _known(state)
    flagged, section, header = [], "", ""
    lines = _prose(report).splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#"):
            section = line.lstrip("# ").strip()
            continue
        if line.lstrip().startswith("|"):
            if i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
                header = line.strip()  # the row above the |---| separator
            units = [f"{line.strip()}  (columns: {header})"]
        else:
            units = re.split(r"(?<=[.!?])\s+", line)
        for unit in units:
            bad = [n for n in _numbers(unit.split("  (columns:")[0]) if n not in known and round(n) not in known]
            text = f"[{section}] {unit.strip()}" if section else unit.strip()
            if bad and text not in flagged:
                flagged.append(text)
    return flagged


class Check(BaseModel):
    sentence: str
    verdict: Literal["derived_ok", "wrong", "unsupported"]
    explanation: str
    correction: str | None


class Verdict(BaseModel):
    checks: list[Check]


verifier = Agent(
    name="Verifier",
    instructions="""\
You check a travel report's numbers against the raw tool results it was built from. You get sentences
whose numbers don't appear verbatim in any tool result, and the tool results.

For each sentence, decide:
- derived_ok: the number is correctly computed from tool results (a sum, ratio, percentage, rounding,
  unit or date arithmetic) or comes from the user's own request (budget, dates, hours).
- wrong: a tool result has this figure but with a different value. Give the right value in `correction`,
  as a rewritten sentence.
- unsupported: nothing in the tool results supports it. `correction`: the sentence without that claim,
  or null to drop it.
Be strict: a count of 1,121 where the tool says 1,799 is wrong.""",
    model=MODEL,
    model_settings=SETTINGS,
    output_type=Verdict,
)


def _tool_results(state: Research) -> str:
    parts = []
    for c in state.tool_calls:
        if c["name"] in DATA_TOOLS:
            parts.append(f"[{c['agent']}] {c['name']}({json.dumps(c['args'], ensure_ascii=False)}):\n{c['result']}")
    return "\n\n".join(parts)


async def verify(report: str, state: Research) -> list[Check]:
    """The problems in the draft: wrong or unsupported numbers. Empty if it checks out."""
    flagged = unsupported_sentences(report, state)
    if not flagged:
        return []
    prompt = (f"Trip brief:\n{json.dumps(state.brief)}\n\nSentences to check:\n"
              + "\n".join(f"- {s}" for s in flagged) + f"\n\nTool results:\n{_tool_results(state)}")
    result = await Runner.run(verifier, prompt, context=state, hooks=state.hooks, max_turns=1)
    return [c for c in result.final_output.checks if c.verdict != "derived_ok"]
