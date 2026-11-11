"""Run the README's sample questions: expected answer, agent answer, and no-tools baseline.

    uv run scripts/run_samples.py            # all samples
    uv run scripts/run_samples.py q2 vague   # some of them

For each sample this writes docs/samples/<id>.json and <id>.md with:
  - expected: the key numbers computed directly with the data tools (the ground truth to compare to)
  - agent:    the full NomadReady answer (same pipeline as /chat), with run stats
  - baseline: the same model (Gemini 3.5 Flash on Vertex AI) with no tools and no system prompt

Google's terms don't allow storing Places content, so coworking place names are removed from the
saved agent answers (counts stay).
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import litellm  # noqa: E402

from scout.config import FALLBACK_LOCATION, PRIMARY_LOCATION  # noqa: E402
from scout.pipeline import answer  # noqa: E402
from tools.advisory import travel_advisory  # noqa: E402
from tools.overlap import work_overlap  # noqa: E402
from tools.stays import find_stays  # noqa: E402
from tools.weather import month_climate  # noqa: E402

OUT = ROOT / "docs" / "samples"
MODEL = "vertex_ai/gemini-3.5-flash"
EASTERN = ("America/New_York", "09:00", "17:00")


def j(text: str) -> dict:
    return json.loads(text)


def stays_summary(r: dict, top_by: str = "n_listings", n: int = 3, min_listings: int = 1) -> dict:
    rows = [x for x in r["by_neighbourhood"] if x["n_listings"] >= min_listings]
    rows.sort(key=lambda x: -(x[top_by] or 0))
    return {k: r.get(k) for k in ("n_available", "n_within_budget", "median_total_usd", "p10_total_usd")} | {
        "top_neighbourhoods": [{k: x[k] for k in ("neighbourhood", "n_listings", "median_total_usd",
                                                   "median_down_mbps_fixed", "share_low_confidence")} for x in rows[:n]]}


# --- Expected answers, straight from the tools (deterministic except the live advisory) ---


def expected_q1() -> dict:
    out = {}
    for city in ("Lisbon", "Mexico City", "Buenos Aires"):
        r = j(find_stays(city, "2027-02-01", "2027-03-01", max_total_usd=2500, needs_workspace=True,
                         room_type="Entire home/apt", min_bedrooms=1))
        o = j(work_overlap(city, *EASTERN, "2027-02-15"))
        w = j(month_climate(city, 2))
        a = j(travel_advisory(city))
        out[city] = stays_summary(r) | {
            "work_hours_local": o["work_hours_local"], "hours_outside_8am_10pm": o["hours_outside_8am_10pm_local"],
            "feb_high_c": w["avg_high_c"], "feb_low_c": w["avg_low_c"], "feb_rainy_days": w["rainy_days_per_month"],
            "advisory_level": a.get("level")}
    return out


def expected_q2() -> dict:
    r = j(find_stays("Mexico City", "2027-03-01", "2027-03-29", max_total_usd=1500))
    # "Fastest" among neighbourhoods with a real choice of listings (20+ within budget)
    return stays_summary(r, top_by="median_down_mbps_fixed", n=5, min_listings=20)


def expected_q3() -> dict:
    metro = j(find_stays("Lisbon", "2027-01-01", "2027-07-02"))
    city = j(find_stays("Lisbon", "2027-01-01", "2027-07-02", neighbourhood="Lisboa"))
    w = j(month_climate("Lisbon", 1))
    return {"metro_area": stays_summary(metro), "city_proper_lisboa": stays_summary(city),
            "jan_high_c": w["avg_high_c"], "jan_low_c": w["avg_low_c"],
            "coworking": "Live from Google Maps at answer time (not stored, per Google's terms).",
            "not_in_data": ["visa rules / how long a US citizen can stay",
                            "coworking prices, desk availability, community"]}


SAMPLES = {
    "q1": {"title": "Full comparison",
           "question": "A month in February, $2,500/month for a 1BR with a dedicated workspace, online 9am–5pm "
                       "Eastern. Compare Lisbon, Mexico City, and Buenos Aires.",
           "sources": "listings + availability (Inside Airbnb) ⨝ speed_tiles (Ookla) on quadkey; climate "
                      "(Open-Meteo); State Dept advisories API; fx_rates; IANA time zones",
           "expected": expected_q1},
    "q2": {"title": "Neighbourhood drill-down",
           "question": "In Mexico City, which neighborhoods have month-long rentals in March under $1,500 and the "
                       "fastest home internet?",
           "sources": "listings + availability ⨝ speed_tiles on quadkey, grouped by neighbourhood",
           "expected": expected_q2},
    "q3": {"title": "Partially answerable",
           "question": "Can I stay in Lisbon for 6 months starting in January, and is the coworking scene good?",
           "sources": "listings + availability; climate; Google Places API (live); visas: not in any source",
           "expected": expected_q3},
    "vague": {"title": "Vague question (clarification)",
              "turns": ["Where should I go somewhere warm and cheap?",
                        "A month starting in February, about $1,800 total, online 9-5 Eastern, and I need a desk."]},
}


# --- Agent and baseline ---


async def run_agent(turns: list[str]) -> list[dict]:
    conversation, out = [], []
    for text in turns:
        conversation.append({"role": "user", "content": text})
        t = time.time()
        response, state = await answer(conversation)
        conversation.append({"role": "assistant", "content": response})
        out.append({"user": text, "response": redact_places(response, state), "seconds": round(time.time() - t),
                    "clarifying": not state.brief, "n_tool_calls": len(state.tool_calls),
                    "agents": sorted({c["agent"] for c in state.tool_calls} | ({"Writer"} if state.brief else set()))})
    return out


def redact_places(text: str, state) -> str:
    names = set()
    for c in state.tool_calls:
        if c["name"] == "nearby_coworking":
            try:
                names |= {p["name"] for p in json.loads(c["result"]).get("places", []) if p.get("name")}
            except (ValueError, TypeError):
                pass
    return strip_places_content(text, names)


def strip_places_content(text: str, names: set[str] = frozenset()) -> str:
    """Remove place names, star ratings and review counts (Google Places content); keep counts of
    spaces found, which are tallies of our own search results."""
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(re.escape(name), "[a coworking space]", text)
    text = re.sub(r"(?:rated |a |an )?\d(?:\.\d)?[- ]?stars?(?: rating)?(?: (?:across|with|from) [\d,]+ (?:reviews|ratings))?",
                  "[rating omitted]", text)
    return re.sub(r"\b[\d,]+ (?:Google )?(?:reviews|ratings)\b", "[rating omitted]", text)


async def baseline(question: str) -> str:
    """Same model, no tools, no system prompt: just the question."""
    for loc in (PRIMARY_LOCATION, FALLBACK_LOCATION):
        try:
            r = await litellm.acompletion(model=MODEL, messages=[{"role": "user", "content": question}],
                                          vertex_location=loc, num_retries=2, timeout=120)
            return r.choices[0].message.content
        except (litellm.RateLimitError, litellm.ServiceUnavailableError, litellm.Timeout):
            continue
    return "(baseline call failed)"


def markdown(sid: str, s: dict, rec: dict) -> str:
    lines = [f"# {s['title']}", ""]
    if "question" in s:
        lines += [f"**Question:** {s['question']}", "", f"**Data:** {s['sources']}", "",
                  "## Expected (computed directly with the data tools)", "", "```json",
                  json.dumps(rec["expected"], indent=1, ensure_ascii=False), "```", ""]
    for i, turn in enumerate(rec["agent"], 1):
        lines += [f"## NomadReady, turn {i}" if len(rec["agent"]) > 1 else "## NomadReady",
                  f"*User:* {turn['user']}", "",
                  f"*{turn['seconds']} s, {turn['n_tool_calls']} tool calls, agents: {', '.join(turn['agents']) or 'Lead'}*",
                  "", turn["response"], ""]
    if rec.get("baseline"):
        lines += ["## Same model, no tools, no system prompt", "", rec["baseline"], ""]
    return "\n".join(lines)


async def main(ids: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for sid in ids:
        s = SAMPLES[sid]
        print(f"[{sid}] {s['title']}", flush=True)
        rec = {"id": sid, "title": s["title"]}
        if "question" in s:
            rec |= {"question": s["question"], "sources": s["sources"], "expected": s["expected"]()}
            rec["agent"] = await run_agent([s["question"]])
            rec["baseline"] = await baseline(s["question"])
        else:
            rec["agent"] = await run_agent(s["turns"])
        for t in rec["agent"]:
            print(f"  agent: {t['seconds']} s, {t['n_tool_calls']} tool calls, clarifying={t['clarifying']}", flush=True)
        (OUT / f"{sid}.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False, default=str))
        (OUT / f"{sid}.md").write_text(markdown(sid, s, rec))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or list(SAMPLES)))
