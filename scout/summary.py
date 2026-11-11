"""At-a-glance numbers for each city card on the page, taken straight from the tool results.

The Researchers retype numbers into their findings (and have garbled them before), so the page's
big numbers are read from the tool calls themselves: the city-wide find_stays call that best
matches the brief, the work_overlap and travel_advisory results, and the climate table. The fit
label and recommended neighbourhoods come from the findings (after reconcile()).
"""

import json
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

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


def _climate(city: str, slug: str, state: Research) -> dict | None:
    months = rows("select month, avg_high_c, avg_low_c, rainy_days_per_month, avg_daylight_hours_per_day, "
                  "avg_sunshine_hours_per_day, years from climate where city = $1 order by month", [city])
    if len(months) != 12:
        return None
    cites = [i for _, r in _results(state, "month_climate", city) for i in _ids(r)]
    return {"years": months[0]["years"], "source": "Open-Meteo (ERA5), monthly averages",
            "slug": slug, "cites": list(dict.fromkeys(cites)),
            "months": [{"m": r["month"], "hi": r["avg_high_c"], "lo": r["avg_low_c"],
                        "rain": r["rainy_days_per_month"], "daylight": r["avg_daylight_hours_per_day"],
                        "sun": r["avg_sunshine_hours_per_day"]} for r in months]}


def sun_times(lat: float, lon: float, day: date, tz: str) -> tuple[datetime, datetime]:
    """Sunrise and sunset in local time, from NOAA's solar equations (good to a minute or two
    away from the poles). https://gml.noaa.gov/grad/solcalc/solareqns.PDF"""
    g = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    phi = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl)) - math.tan(phi) * math.tan(decl)
    ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
    midnight = datetime.combine(day, time(0), timezone.utc)
    rise = midnight + timedelta(minutes=720 - 4 * (lon + ha) - eqtime)
    sets = midnight + timedelta(minutes=720 - 4 * (lon - ha) - eqtime)
    return rise.astimezone(ZoneInfo(tz)), sets.astimezone(ZoneInfo(tz))


def _daylight(c: dict, brief: dict, state: Research) -> dict | None:
    """Sunrise, sunset and hours of daylight in the city during the stay (its first, middle and
    last day), registered as a citation so the page can cite it like a tool result."""
    try:
        first = date.fromisoformat(brief["check_in"])
        last = date.fromisoformat(brief["check_out"]) - timedelta(days=1)
    except (KeyError, TypeError, ValueError):
        return None
    lat, lon = c.get("center_lat"), c.get("center_lon")
    if lat is None or lon is None:
        return None

    def at(day: date) -> dict:
        rise, sets = sun_times(lat, lon, day, c["timezone"])
        return {"date": day.isoformat(), "sunrise": f"{rise:%H:%M}", "sunset": f"{sets:%H:%M}",
                "hours": round((sets - rise).total_seconds() / 3600, 1)}

    cid = f"solar-{c['slug']}"
    state.citations.setdefault(cid, {
        "source": "Computed with NOAA's solar equations",
        "detail": f"sunrise and sunset at {c['city']}'s centre ({lat:.3f}, {lon:.3f}), {c['timezone']}",
        "url": "https://gml.noaa.gov/grad/solcalc/solareqns.PDF"})
    return {"first": at(first), "mid": at(first + (last - first) / 2), "last": at(last), "cites": [cid]}


def _neighbourhoods(neighbourhoods, state: Research) -> list[dict]:
    """The recommended neighbourhoods (numbers already checked against the tool results by
    reconcile()) with their example listings' details."""
    examples = listing_examples(state)
    return [{"name": n.name, "n_within_budget": n.n_within_budget, "median_total_usd": n.median_total_usd,
             "median_down_mbps_fixed": n.median_down_mbps_fixed, "low_confidence_speed": n.low_confidence_speed,
             "coworking_nearby": n.coworking_nearby,
             "examples": [{"url": u, **examples[u]} for u in n.example_listing_urls if u in examples]}
            for n in neighbourhoods]


def listing_examples(state: Research) -> dict[str, dict]:
    """Details of each example listing the findings link to (url -> kind, price, room type, cites),
    so the Writer can label the links with numbers from the tool results."""
    wanted = {u for f in state.findings.values() for n in f.neighbourhoods for u in n.example_listing_urls}
    out: dict[str, dict] = {}
    for call in state.tool_calls:
        if call["name"] != "find_stays":
            continue
        try:
            result = json.loads(call["result"])
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict):
            continue
        nights = (result.get("stay") or {}).get("nights")
        for ex in result.get("examples", []):
            url = ex.get("listing_url")
            if url in wanted and url not in out:
                out[url] = {k: ex.get(k) for k in ("kind", "neighbourhood", "room_type", "bedrooms", "total_usd",
                                                   "nightly_usd", "neighbourhood_median_usd")}
                out[url] |= {"nights": nights, "cites": _ids(result, "airbnb-")}
    return out


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
            "climate": _climate(city, c["slug"], state),
            "daylight": _daylight(c, state.brief, state),
            "neighbourhoods": _neighbourhoods(neighbourhoods, state),
        })
    return cards
