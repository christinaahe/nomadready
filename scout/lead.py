"""Lead, phase 2: fan out one City Researcher per city in parallel, check the findings for gaps,
and send targeted follow-ups, within the caps in config.py."""

import json

from agents import Agent, ModelSettings, RunContextWrapper, Runner, function_tool
from agents.run_error_handlers import RunErrorHandlerInput

from scout.config import (MAX_RESEARCHERS, MAX_ROUNDS, MODEL, RESEARCH_DEADLINE_S, RESEARCHER_MAX_TURNS,
                          SETTINGS)
from scout.researcher import researcher, work_overlap
from scout.state import Research
from tools.db import city_info, fold, rows


WRAP_UP = ("You're out of turns. Don't call any more tools. Return your findings now from the tool results "
           "you already have, and list in `gaps` whatever you didn't get to check.")


@function_tool
async def research_city(ctx: RunContextWrapper[Research], city: str, task: str) -> str:
    """Send a City Researcher (a fresh agent with the data tools) to research one city for the brief.
    Call it once per city in the same turn so they run in parallel. Returns its findings as JSON.

    Args:
        city: One covered city, e.g. 'Lisbon'.
        task: What to find. For a first look: 'Full first look for the brief.' For a follow-up, the
              specific gap, e.g. 'Fewer than 5 listings under budget: check private rooms and the
              neighbourhoods just outside the center.'
    """
    state = ctx.context
    try:
        name = city_info(city)["city"]
    except LookupError as e:
        return json.dumps({"error": str(e)})
    if state.researchers >= MAX_RESEARCHERS:
        return json.dumps({"error": f"Researcher limit ({MAX_RESEARCHERS}) reached. Stop and reply with your notes."})
    if state.elapsed() > RESEARCH_DEADLINE_S:
        return json.dumps({"error": "Out of time for more research. Stop and reply with your notes."})
    rounds = state.research_rounds | {state.lead_turn}
    if len(rounds) > MAX_ROUNDS:
        return json.dumps({"error": f"Follow-up round limit reached ({MAX_ROUNDS - 1} follow-up rounds). "
                                    "Stop and reply with your notes."})
    state.research_rounds = rounds
    state.researchers += 1
    state.researcher_runs[name] = state.researcher_runs.get(name, 0) + 1
    n = state.researcher_runs[name]
    agent_name = f"Researcher-{name}" + (f" #{n}" if n > 1 else "")

    # What researcher.as_tool() does, written out: a fresh context per researcher, named per city.
    agent = researcher.clone(name=agent_name)
    prompt = f"Trip brief:\n{json.dumps(state.brief, indent=1)}\n\nCity: {name}\nTask: {task}"
    wrapped_up = []

    async def wrap_up(data: RunErrorHandlerInput):
        """Out of turns: one more turn, tools off, to report what it already found instead of
        throwing that work away."""
        final = data.run_data.last_agent.clone(model_settings=SETTINGS.resolve(ModelSettings(tool_choice="none")))
        r = await Runner.run(final, data.run_data.history + [{"role": "user", "content": WRAP_UP}],
                             context=state, hooks=state.hooks, max_turns=1)
        wrapped_up.append(True)
        return r.final_output

    try:
        result = await Runner.run(agent, prompt, context=state, hooks=ctx.context.hooks,
                                  max_turns=RESEARCHER_MAX_TURNS, error_handlers={"max_turns": wrap_up})
    except Exception as e:  # a researcher that fails is a gap, not a crashed answer
        return json.dumps({"city": name, "error": f"Researcher failed: {type(e).__name__}: {str(e)[:300]}"})
    findings = reconcile(result.final_output, state, agent_name)
    if wrapped_up:
        findings.gaps.append("The researcher ran out of turns; these findings cover only what it checked.")
    state.findings[agent_name] = findings
    return findings.model_dump_json()


FIELDS = (("n_within_budget", "n_listings"), ("median_total_usd", "median_total_usd"),
          ("median_down_mbps_fixed", "median_down_mbps_fixed"))


def reconcile(findings, state: Research, agent_name: str):
    """Fix neighbourhood numbers a researcher garbled while copying (seen in testing: 1,121 for a
    tool's 1,799), and fill in missing example links.

    A researcher may call find_stays several times with different filters, so a reported number is
    kept if it matches ANY of its own find_stays rows for that neighbourhood. Only a number that
    matches none is replaced, with the row from a call that used the brief's budget."""
    rows_for: dict[str, list[tuple[dict, bool]]] = {}  # neighbourhood -> [(row, used the brief's budget)]
    examples: dict[str, list[str]] = {}
    budget = state.brief.get("budget_total_usd")
    for call in state.tool_calls:
        if call["agent"] != agent_name or call["name"] != "find_stays":
            continue
        try:
            data = json.loads(call["result"])
        except ValueError:
            continue
        on_budget = isinstance(call["args"], dict) and call["args"].get("max_total_usd") == budget
        for row in data.get("by_neighbourhood", []):
            rows_for.setdefault(fold(row["neighbourhood"]), []).append((row, on_budget))
        for ex in data.get("examples", []):
            examples.setdefault(fold(ex["neighbourhood"]), []).append(ex["listing_url"])

    fixed = []
    for nb in findings.neighbourhoods:
        candidates = rows_for.get(fold(nb.name), [])
        preferred = next((r for r, on_budget in candidates if on_budget), None)
        for field, key in FIELDS:
            reported = getattr(nb, field)
            if reported is None or not candidates or any(_same(reported, r[key]) for r, _ in candidates):
                continue
            if preferred is not None:
                fixed.append(f"{nb.name} {field}: {reported} -> {preferred[key]}")
                setattr(nb, field, preferred[key])
        if preferred is not None:
            nb.low_confidence_speed = (preferred.get("share_low_confidence") or 0) > 0.3
        if not nb.example_listing_urls:
            nb.example_listing_urls = examples.get(fold(nb.name), [])[:2]
    state.corrections.extend(f"{agent_name}: {f}" for f in fixed)
    return findings


def _same(a, b) -> bool:
    return b is not None and abs(float(a) - float(b)) < 0.51  # rounding


def _lead_instructions(ctx: RunContextWrapper[Research], agent) -> str:
    cities = rows("select city, country, timezone, currency from cities order by city")
    covered = "; ".join(f"{c['city']} ({c['country']}, {c['timezone']})" for c in cities)
    return f"""\
You lead a research team helping a remote worker choose where to stay. You get a trip brief.

1. Choose cities. Use the brief's cities if it names any (all of them, up to 4; one is fine if the user
   asked about one). Otherwise pick 3-4 from the covered cities to fit the brief (if uncovered_cities is
   set, pick covered cities most like those): work hours vs time zone (check with work_overlap if unsure),
   budget, preferences like warm or cheap.
   Covered cities: {covered}.
2. Call research_city once per city, ALL IN THE SAME TURN, so the researchers work in parallel. Task:
   'Full first look for the brief.'
3. Read the findings and look for gaps:
   - Fewer than ~5 listings within budget: ask that city's researcher to try other room types,
     neighbourhoods outside the center, or a shorter stay to split the trip across two places.
   - Speed in a recommended neighbourhood is low-confidence: ask for neighbourhood_speed there.
   - Work hours fall mostly outside 08:00-22:00 local in every city: add a city in a closer time zone.
   - The brief has wants_coworking, or a recommended stay has no dedicated workspace (shared room, or
     listings without one), and coworking wasn't searched: ask for nearby_coworking in the recommended
     neighbourhoods.
   - A tool failed: ask once more, or accept it as unknown.
   Send all follow-ups in one turn. At most {MAX_ROUNDS - 1} follow-up rounds.
4. When there are no gaps worth chasing, reply with short notes for the writer (no research tools):
   - the ranking you suggest and why, in a line per city;
   - which parts of the user's request the data can't answer (see the brief's other_questions,
     uncovered_cities, and anything about visas, safety of a neighbourhood, nightlife, current prices).
Never state facts yourself: the writer uses only the researchers' findings."""


lead = Agent(
    name="Lead",
    instructions=_lead_instructions,
    model=MODEL,
    model_settings=SETTINGS,
    tools=[research_city, work_overlap],
)
