"""At-a-glance numbers for each city card on the page, taken straight from the tool results.

The Researchers retype numbers into their findings (and have garbled them before), so the page's
big numbers are read from the tool calls themselves: the city-wide find_stays call that best
matches the brief, the work_overlap and travel_advisory results, and the climate table. The fit
label and recommended neighbourhoods come from the findings (after reconcile()).
"""

import json

from tools.db import city_info, rows
from scout.state import Research


def _results(state: Research, tool: str, city: str) -> list[tuple[dict, dict]]:
    """(args, parsed result) for each successful call of `tool` about `city`, in call order."""
    out = []
    for call in state.tool_calls:
        if call["name"] != tool:
            continue
        args = call["args"] if isinstance(call["args"], dict) else {}
        try:
            if city_info(str(args.get("city", "")))["city"] != city:
                continue
            result = json.loads(call["result"])
        except (LookupError, TypeError, ValueError):
            continue
        if isinstance(result, dict) and not result.get("error"):
            out.append((args, result))
    return out


def _ids(result: dict, prefix: str = "") -> list[str]:
    return [c["id"] for c in result.get("citations", []) if c.get("id", "").startswith(prefix)]


def _stays(state: Research, city: str) -> tuple[dict | None, list[str]]:
    """The find_stays call that best matches the brief: city-wide, the brief's budget and filters.
    Returns (stays summary, ookla citation ids)."""
    brief = state.brief

    def score(args: dict) -> int:
        return (8 * (not args.get("neighbourhood"))
                + 4 * (args.get("max_total_usd") == brief.get("budget_total_usd"))
                + 2 * ((args.get("room_type") or None) == (brief.get("room_type") or None))
                + 1 * (bool(args.get("needs_workspace")) == bool(brief.get("needs_workspace"))))

    calls = _results(state, "find_stays", city)
    if not calls:
        return None, []
    # Ties go to the broadest call: a researcher may never search fully city-wide, and then the
    # city-proper group ('Lisboa') beats a single neighbourhood ('Penha de França')
    args, r = max(calls, key=lambda c: (score(c[0]), c[1].get("n_available") or 0))
    return {
        "n_available": r.get("n_available"),
        "n_within_budget": r.get("n_within_budget") if args.get("max_total_usd") else None,
        "median_total_usd": r.get("median_total_usd"),
        "p10_total_usd": r.get("p10_total_usd"),
        "budget_usd": args.get("max_total_usd"),
        "nights": (r.get("stay") or {}).get("nights"),
        "scope": None if not args.get("neighbourhood") else args["neighbourhood"],
        "cites": _ids(r, "airbnb-"),
    }, _ids(r, "ookla-")


def _climate(city: str, slug: str) -> dict | None:
    months = rows("select month, avg_high_c, avg_low_c, rainy_days_per_month, years from climate "
                  "where city = $1 order by month", [city])
    if len(months) != 12:
        return None
    return {"years": months[0]["years"], "source": "Open-Meteo (ERA5), monthly averages",
            "slug": slug,
            "months": [{"m": r["month"], "hi": r["avg_high_c"], "lo": r["avg_low_c"],
                        "rain": r["rainy_days_per_month"]} for r in months]}


def city_cards(state: Research) -> list[dict]:
    """One summary per researched city, for the page's stat tiles and weather strip."""
    by_city: dict[str, list] = {}
    for findings in state.findings.values():  # in order: first look, then follow-ups
        by_city.setdefault(findings.city, []).append(findings)

    cards = []
    for name, runs in by_city.items():
        try:
            c = city_info(name)
        except LookupError:
            continue
        city = c["city"]
        latest = runs[-1]
        fit = next((f.fit for f in reversed(runs) if f.fit != "unknown"), latest.fit)
        neighbourhoods = next((f.neighbourhoods for f in reversed(runs) if f.neighbourhoods), [])

        stays, ookla_ids = _stays(state, city)
        speeds = [n.median_down_mbps_fixed for n in neighbourhoods if n.median_down_mbps_fixed]
        overlap = _results(state, "work_overlap", city)
        advisory = _results(state, "travel_advisory", city)

        cards.append({
            "city": city,
            "fit": fit,
            "stays": stays,
            "speed": {"min": min(speeds), "max": max(speeds), "n_areas": len(speeds),
                      "areas": [n.name for n in neighbourhoods], "cites": ookla_ids} if speeds else None,
            "hours": {"local": overlap[-1][1].get("work_hours_local"),
                      "outside": overlap[-1][1].get("hours_outside_8am_10pm_local"),
                      "cites": _ids(overlap[-1][1])} if overlap else None,
            "advisory": {"level": advisory[-1][1].get("level"), "url": advisory[-1][1].get("link"),
                         "cites": _ids(advisory[-1][1])} if advisory else None,
            "climate": _climate(city, c["slug"]),
        })
    return cards
