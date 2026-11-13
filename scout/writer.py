"""Writer: the cited report, from the brief, the Lead's notes and the Researchers' findings only.

Citations are checked in code: every [id] in the report must be an id some tool returned, and the
Sources list is built from those tool citations, not written by the model.
"""

import asyncio
import json
import re

from agents import Agent, Runner

from scout.config import MODEL, REVISION_CUTOFF_S, SETTINGS, WRITER_DEADLINE_S
from scout.state import Research
from scout.summary import city_cards, listing_examples
from scout.verify import verify

MIN_WRITE_S = 30  # the draft always gets at least this long

writer = Agent(
    name="Writer",
    instructions="""\
Write the answer for a remote worker choosing where to stay, in markdown, using ONLY the trip brief,
the lead's notes and the researchers' findings you're given.

Structure:
1. If the brief lists assumptions, start with one line: "Assumed: ...".
2. **Bottom line**: 2-4 sentences ranking the cities for this brief. If the brief has uncovered_cities,
   start by saying those cities aren't among the 20 cities NomadReady's data covers (not that they
   weren't researched), so you can't compare them.
3. A section per city, best first: "## <City>: <one-line verdict>". Then bullets for estimated cost of
   the stay, work hours in local time, typical weather, travel advisory level (with its link). Then a
   table of the recommended neighbourhoods:
   | Neighbourhood | Listings in budget | Median stay total | Median download (fixed) | Speed confidence |
   If budget_total_usd is null, call the second column "Listings available" instead. If the brief's
   rank_neighbourhoods_by is set, order the rows by that measure (fastest, cheapest, or most listings
   first) and say in a sentence that they're ranked that way. Add a "Coworking
   nearby" column only if coworking was searched (coworking_nearby not null). Then, per neighbourhood,
   its example listings from "Example listings" below: the affordable one, then the typical one (near
   that neighbourhood's median), each as a link labelled with its room type and stay total, e.g.
   "- Roma Norte: affordable [Private room, $612](url) [airbnb-mexico-city] · typical [Entire home, 1 BR,
   $1,061](url) [airbnb-mexico-city]". bedrooms 0 means a studio. Round dollars and Mbps to whole numbers ($1,053, 201 Mbps).
   Temperatures in °F, rounded to whole degrees (highs around 62°F).
4. "## What this data can't tell you": the parts of the request outside the data (from the lead's
   notes and the brief's other_questions / uncovered_cities), plus, always: prices are the September
   snapshot's nightly rate x nights, not live quotes, and don't include weekly/monthly discounts;
   availability may have changed; speeds are measured around the listing, not the unit's Wi-Fi.

Citations: after every number, put the citation id(s) it came from in square brackets, exactly as given
in the findings' `cites`, e.g. "$2,149 [airbnb-lisbon]" or "214 Mbps [ookla-fixed-2026-Q3]". Use only
ids from the citation list; researcher names like "Researcher-Lisbon" are not citations. Don't write a
Sources section: it's added for you.

For cost, lead with the median and the 10th-percentile stay total; don't headline the single cheapest
listing (it's often a data oddity).

Write plain, specific sentences. If findings disagree or are missing, say so; never fill gaps from
your own knowledge. Say "room" or "shared room" when a listing isn't a whole apartment.""",
    model=MODEL,
    model_settings=SETTINGS,
)

# [airbnb-lisbon] or [airbnb-lisbon, ookla-fixed-2026-Q3], but not markdown links [text](url)
CITE_RE = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9_.-]*(?:\s*[,;]\s*[A-Za-z0-9][A-Za-z0-9_.-]*)*)\](?!\()")


def cited_ids(markdown: str) -> list[str]:
    ids = []
    for group in CITE_RE.findall(markdown):
        for token in re.split(r"\s*[,;]\s*", group):
            if "-" in token and token not in ids:  # every citation id has a dash; plain [words] don't
                ids.append(token)
    return ids


def sources_section(ids: list[str], citations: dict[str, dict]) -> str:
    lines = ["", "## Sources"]
    for cid in ids:
        c = citations[cid]
        url = f" <{c['url']}>" if c.get("url") else ""
        lines.append(f"- **[{cid}]** {c['source']}: {c['detail']}{url}")
    return "\n".join(lines)


def _writer_input(state: Research, lead_notes: str) -> str:
    findings = {name: f.model_dump() if hasattr(f, "model_dump") else f for name, f in state.findings.items()}
    citation_list = "\n".join(f"- {cid}: {c['source']} ({c['detail']})" for cid, c in state.citations.items())
    # City-level numbers read straight from the tool results: the page shows these as stat tiles, so
    # the prose must use the same ones (a researcher may have dropped or retyped them)
    try:
        exact = [{k: v for k, v in card.items() if k != "climate"} for card in city_cards(state)]
    except Exception:
        exact = []
    try:
        examples = listing_examples(state)
    except Exception:
        examples = {}
    return (f"Trip brief:\n{json.dumps(state.brief, indent=1)}\n\n"
            f"Lead's notes:\n{lead_notes}\n\n"
            f"Researchers' findings (follow-ups are '#2', '#3'):\n{json.dumps(findings, indent=1, default=str)}\n\n"
            "Exact city-level numbers from the tool results (shown to the reader as headline figures; use "
            "these for each city's cost, counts, speed range, work hours and advisory, with their `cites`):\n"
            f"{json.dumps(exact, indent=1, default=str)}\n\n"
            "Example listings (url -> details from the tool results; label the links with these):\n"
            f"{json.dumps(examples, indent=1, default=str)}\n\n"
            f"Citation ids you may use:\n{citation_list}")


def _plain_summary(state: Research) -> str:
    """If the Writer can't finish in time: each researcher's own summary and gaps, unpolished."""
    parts = ["The full report took too long to write, so here are the researchers' findings as they "
             "returned them (numbers are from the data tools; see the tool calls for details)."]
    for name, f in state.findings.items():
        parts.append(f"\n## {f.city}: {f.fit} fit\n\n{f.summary}")
        if f.gaps:
            parts.append("\nGaps: " + "; ".join(f.gaps))
    return "\n".join(parts)


async def write_report(state: Research, lead_notes: str) -> str:
    hooks = state.hooks
    prompt = _writer_input(state, lead_notes)
    hooks.phase("writing", f"Writing the report from {len(state.findings)} researchers' findings")
    try:
        result = await asyncio.wait_for(Runner.run(writer, prompt, context=state, hooks=hooks, max_turns=1),
                                        timeout=max(WRITER_DEADLINE_S - state.elapsed(), MIN_WRITE_S))
    except TimeoutError:
        hooks.send({"type": "agent", "agent": "Writer", "status": "finished"})
        return _plain_summary(state)
    report = str(result.final_output)

    # One revision, if the Verifier finds wrong or unsupported numbers or the draft cites unknown ids.
    # Late in the run there's no time for a model check and a rewrite: keep the code-only checks.
    late = state.elapsed() > REVISION_CUTOFF_S
    hooks.phase("checking", "Checking citations (out of time for the full fact-check)" if late
                else "Fact-checking every number against the tool results")
    checks = [] if late else await verify(report, state)
    problems = [f"- \"{c.sentence}\": {c.verdict}. {c.explanation}"
                + (f" Use instead: \"{c.correction}\"" if c.correction else " Remove it.")
                for c in checks]
    unknown = [cid for cid in cited_ids(report) if cid not in state.citations]
    if unknown:
        problems.append(f"- These citation ids aren't in the citation list: {', '.join(unknown)}. Use only listed ids.")
    hooks.send({"type": "checked", "problems": [{"sentence": c.sentence, "verdict": c.verdict,
                                                 "explanation": c.explanation} for c in checks],
                "unknown_citations": unknown})
    if problems and not late:
        hooks.phase("revising", f"Fixing {len(problems)} problem(s) the fact-check found")
        fix = (f"{prompt}\n\nYour draft:\n{report}\n\nA fact-check against the raw data found:\n"
               + "\n".join(problems) + "\n\nReturn the full corrected report, changing only what's needed.")
        try:
            result = await asyncio.wait_for(Runner.run(writer, fix, context=state, hooks=hooks, max_turns=1),
                                            timeout=max(WRITER_DEADLINE_S - state.elapsed(), MIN_WRITE_S))
            report = str(result.final_output)
        except TimeoutError:  # keep the draft; unknown citations are still marked below
            hooks.send({"type": "agent", "agent": "Writer", "status": "finished"})
    for cid in cited_ids(report):
        if cid not in state.citations:  # still unknown: mark it rather than pass it off as a source
            report = report.replace(f"[{cid}]", "[unverified]")

    used = [cid for cid in cited_ids(report) if cid in state.citations]
    return report.rstrip() + "\n" + sources_section(used, state.citations)
