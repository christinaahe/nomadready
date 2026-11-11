"""Readable progress lines for the streaming page: what each tool call is doing, and what it found.

Only used for display. Numbers here are read straight from the tool results; nothing is computed
that the report relies on.
"""

import json


def _city(args: dict) -> str:
    return args.get("city") or args.get("city_or_currency") or ""


def _where(args: dict) -> str:
    nb = args.get("neighbourhood")
    return f"{_city(args)} / {nb}" if nb else _city(args)


def describe_start(tool: str, args: dict) -> str:
    a = args if isinstance(args, dict) else {}
    match tool:
        case "research_city":
            return f"sending a researcher to {a.get('city', '?')}: {a.get('task', '')}"
        case "find_stays":
            bits = [f"{a.get('check_in', '?')} to {a.get('check_out', '?')}"]
            if a.get("max_total_usd"):
                bits.append(f"up to ${a['max_total_usd']:,.0f}")
            if a.get("room_type"):
                bits.append(a["room_type"].lower())
            if a.get("needs_workspace"):
                bits.append("with a workspace")
            return f"searching stays in {_where(a)} ({', '.join(bits)})"
        case "neighbourhood_speed":
            return f"checking measured internet speeds in {_where(a)}"
        case "month_climate":
            return f"looking up {a.get('month', '')} weather in {_city(a)}"
        case "travel_advisory":
            return f"checking the travel advisory for {_city(a)}"
        case "fx_to_usd":
            return f"getting the exchange rate for {_city(a)}"
        case "work_overlap":
            return f"converting {a.get('work_start', '?')}-{a.get('work_end', '?')} {a.get('home_tz', '')} to {_city(a)} time"
        case "nearby_coworking":
            return f"searching Google Maps for coworking near {_where(a)}"
        case "query_listings":
            return "running a custom query on the listings"
    return f"calling {tool}"


def describe_end(tool: str, args: dict, result: str) -> str:
    try:
        r = json.loads(result)
    except (TypeError, ValueError):
        return "done"
    if not isinstance(r, dict):
        return "done"
    if r.get("error"):
        return f"error: {str(r['error'])[:160]}"
    a = args if isinstance(args, dict) else {}
    try:
        match tool:
            case "research_city":
                nbs = [n.get("name") for n in r.get("neighbourhoods", [])][:3]
                return f"{r.get('city', a.get('city'))}: {r.get('fit', '?')} fit" + (f" (top: {', '.join(nbs)})" if nbs else "")
            case "find_stays":
                text = f"{r.get('n_available', 0):,} stays open"
                if r.get("n_within_budget") is not None and (r.get("filters") or {}).get("max_total_usd"):
                    text += f", {r['n_within_budget']:,} within budget"
                if r.get("median_total_usd"):
                    text += f", median ${r['median_total_usd']:,.0f} for the stay"
                return text
            case "neighbourhood_speed":
                return f"speeds for {len(r.get('neighbourhoods', [])) or 1} neighbourhood(s)"
            case "month_climate":
                return (f"{r.get('month')}: highs {r.get('avg_high_c')}°C, lows {r.get('avg_low_c')}°C, "
                        f"{r.get('rainy_days_per_month')} rainy days")
            case "travel_advisory":
                return f"{r.get('country')}: Level {r.get('level')}"
            case "fx_to_usd":
                return f"1 {r.get('currency')} = ${r.get('usd_per_unit'):.4g}"
            case "work_overlap":
                return f"{r.get('work_hours_local')} local, {r.get('hours_outside_8am_10pm_local')} h outside 8am-10pm"
            case "nearby_coworking":
                return f"{r.get('n_coworking')} coworking spaces, {r.get('n_well_rated')} well rated"
            case "query_listings":
                return f"{len(r.get('rows', []))} rows"
    except (TypeError, ValueError, AttributeError):
        pass
    return "done"
