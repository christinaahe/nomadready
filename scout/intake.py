"""Lead, phase 1: turn the conversation into a trip brief, or ask a clarifying question.

The model only extracts what the user said. Whether that's enough to research, the dates,
and the budget arithmetic are decided here in code, so the clarification gate is predictable.
"""

import calendar
from datetime import date, timedelta
from typing import Literal

from agents import Agent, Runner
from pydantic import BaseModel, Field

from scout.config import MODEL, SETTINGS
from tools.db import city_info, rows

DEFAULT_NIGHTS = 28  # budget arithmetic only, before the length is known
# A season stands for its middle month (northern-hemisphere wording, as most users mean it); the brief says so.
SEASON_MONTH = {"winter": 1, "spring": 4, "summer": 7, "autumn": 10}


class Extracted(BaseModel):
    """What the user has said so far, across the whole conversation. null when not stated."""

    cities: list[str] = Field(description="Cities the user named, as written. Empty if none.")
    check_in: str | None = Field(description="Exact arrival date YYYY-MM-DD, only if the user gave a day.")
    check_out: str | None = Field(description="Exact departure date YYYY-MM-DD, only if given.")
    month: int | None = Field(description="Month of the stay, 1-12, if given without an exact date. Resolve "
                                          "relative phrases with today's date ('next month', 'in January').")
    season: Literal["winter", "spring", "summer", "autumn"] | None = Field(
        description="Season, only if the user gave a season and no month ('this summer', 'in the fall').")
    year: int | None = Field(description="Year, only if the user stated it.")
    nights: int | None = Field(description="Stay length in nights: '2 weeks' = 14, 'a few weeks' = 21, 'a month' = 28, "
                                           "'a couple of months' = 60, '6 months' = 182. Null if no length was given.")
    budget_amount: float | None = Field(description="Budget in US dollars, as a number.")
    budget_period: Literal["stay", "month", "week", "night"] | None = Field(
        description="What the budget covers: the whole stay, per month, per week, or per night.")
    budget_currency: str | None = Field(description="Currency code if the user gave a non-USD budget, else null.")
    home_tz: str | None = Field(description="Home time zone as an IANA name (America/New_York) or as written.")
    work_start: str | None = Field(description="Start of required online hours in home time, HH:MM.")
    work_end: str | None = Field(description="End of required online hours in home time, HH:MM.")
    flexible_hours: bool = Field(description="True if the user said their hours are flexible / async / no fixed hours.")
    needs_workspace: bool = Field(description="True if they want a dedicated workspace / desk in the rental.")
    room_type: Literal["Entire home/apt", "Private room", "Shared room"] | None = Field(
        description="Only if the user asked for one: whole place, private room, or shared room / hostel / dorm.")
    min_bedrooms: int | None = Field(description="Minimum bedrooms if stated: '1BR' = 1, '2 bedrooms' = 2. Null if not.")
    guests: int = Field(description="People staying. 1 unless stated.")
    preferences: list[str] = Field(description="Other wishes that could guide picking cities: warm, cheap, beach, ...")
    wants_coworking: bool = Field(description="True if the user asked about coworking spaces or the coworking scene.")
    other_questions: list[str] = Field(
        description="Anything else asked that isn't about housing, internet, weather, work hours or travel "
                    "advisories, e.g. visas, safety of a neighbourhood, nightlife, coworking prices or community. "
                    "Quote briefly.")
    off_topic: bool = Field(description="True if the message isn't about planning a remote-work stay at all.")


intake_agent = Agent(
    name="Lead",
    instructions=lambda ctx, agent: f"""\
Today is {date.today().isoformat()}. Read the whole conversation and extract the user's remote-work trip:
what they've said in any message, with later messages overriding earlier ones. Don't guess: use null
for anything not stated. Convert spelled-out amounts ("two and a half thousand") to numbers.""",
    model=MODEL,
    model_settings=SETTINGS,
    output_type=Extracted,
)


def _calendar_window() -> tuple[date, date]:
    r = rows("select max(airbnb_snapshot_date) first, (select min(m) from "
             "(select max(end_date) m from availability group by city)) last from cities")[0]
    return max(r["first"], date.today()), r["last"]


def _resolve(x: Extracted) -> tuple[dict, list[str], list[str]]:
    """The brief for the Lead, what's missing, and the assumptions made to fill gaps."""
    missing, assumed = [], []
    first_day, last_day = _calendar_window()

    cities, unknown = [], []
    for name in x.cities:
        try:
            cities.append(city_info(name)["city"])
        except LookupError:
            unknown.append(name)

    # Dates: exact > month > season. Timing and length are always asked for: prices, availability
    # and weather all depend on them, so guessing would answer a different trip.
    check_in = date.fromisoformat(x.check_in) if x.check_in else None
    nights = x.nights
    if x.check_out and check_in:
        nights = (date.fromisoformat(x.check_out) - check_in).days
    month = x.month
    if not check_in and not month and x.season:
        month = SEASON_MONTH[x.season]
        assumed.append(f"\"{x.season}\": assumed {calendar.month_name[month]} (northern-hemisphere seasons)")
    if not check_in and month:
        # The next time that month comes around (a stated year wins; the window check catches bad ones)
        check_in = date(x.year or first_day.year, month, 1)
        if check_in < first_day.replace(day=1) and not x.year:
            check_in = check_in.replace(year=check_in.year + 1)
        check_in = max(check_in, first_day) if check_in.month == first_day.month else check_in
    if not check_in:
        missing.append("roughly when you're going (dates, a month, or a season)")
    if not nights:
        missing.append("roughly how long you'll stay (e.g. 2 weeks, a month, 3 months)")

    budget = None
    if x.budget_amount is not None and x.budget_currency in (None, "USD"):
        n = nights or DEFAULT_NIGHTS
        units = {"stay": 1,
                 "month": 1 if 26 <= n <= 31 else n / 30,  # "$2,500/month" for a 28-night February is $2,500
                 "week": n / 7,
                 "night": n}[x.budget_period or "stay"]
        budget = round(x.budget_amount * units)
    elif x.budget_amount is not None:
        missing.append(f"your budget in US dollars (you gave {x.budget_currency})")
    else:
        missing.append("a budget for the stay in US dollars")

    if not (x.flexible_hours or x.home_tz):
        missing.append("the hours you need to be online and your home time zone (or say they're flexible)")

    # A city named = a specific question: budget and work hours can be left open (stated in the
    # report), but timing and length are still required.
    if cities:
        missing = [m for m in missing if not (m.startswith("a budget") or m.startswith("the hours"))]
        if budget is None and x.budget_amount is None:
            assumed.append("no budget given: not filtering by price")
        if not (x.flexible_hours or x.home_tz):
            assumed.append("no work hours given: work-hours overlap not checked")

    brief = {
        "cities": cities,
        "uncovered_cities": unknown,
        "check_in": check_in.isoformat() if check_in else None,
        "check_out": (check_in + timedelta(days=nights)).isoformat() if check_in and nights else None,
        "nights": nights,
        "month": calendar.month_name[check_in.month] if check_in else None,
        "budget_total_usd": budget,
        "budget_as_stated": f"${x.budget_amount:,.0f} per {x.budget_period or 'stay'}" if x.budget_amount else None,
        "home_tz": x.home_tz,
        "work_hours_home": f"{x.work_start}-{x.work_end}" if x.work_start and x.work_end else None,
        "flexible_hours": x.flexible_hours,
        "needs_workspace": x.needs_workspace,
        # "a 1BR" means a whole 1-bedroom place, not a private room in someone's 1-bedroom flat
        "room_type": x.room_type or ("Entire home/apt" if x.min_bedrooms else None),
        "min_bedrooms": x.min_bedrooms,
        "guests": x.guests or 1,
        "preferences": x.preferences,
        "wants_coworking": x.wants_coworking,
        "other_questions": x.other_questions,
        "assumptions": assumed,
        "calendar_window": f"{first_day.isoformat()} to {last_day.isoformat()}",
    }
    if check_in and nights:
        end = check_in + timedelta(days=nights)
        if nights < 7:
            missing.append("a stay of at least 7 nights (NomadReady covers week-plus stays)")
        elif check_in < first_day or end > last_day:
            missing.append(f"dates between {first_day:%b %-d, %Y} and {last_day:%b %-d, %Y} (the listing data covers that window)")
    return brief, missing, unknown


INTRO = ("I'm NomadReady: I help remote workers pick a city and neighbourhood for a stay of a week or more, "
         "using Airbnb availability, measured internet speeds, weather, and time zones. Tell me when you're "
         "going, your budget, and the hours you need to be online (with your home time zone).")


def _question(missing: list[str], unknown: list[str], cities: list[str] = ()) -> str:
    parts = []
    if unknown:
        covered = [r["city"] for r in rows("select city from cities order by city")]
        parts.append(f"I don't have data for {', '.join(unknown)}. I cover: {', '.join(covered)}.")
    if missing and cities:
        parts.append(f"To look at {', '.join(cities)} I need {'; and '.join(missing)}: prices, availability "
                     "and weather depend on it. Rough is fine, e.g. \"about 3 weeks in March\" or \"a month "
                     "this summer\". A budget and the hours you need to be online (with your time zone) help too.")
    elif missing:
        ask = "; ".join(missing)
        parts.append(f"To compare cities for you I need a bit more: {ask}.")
        parts.append('For example: "4 weeks from Feb 1, about $2,000 total, online 9am-5pm US Eastern, '
                     'need a desk." You can also name cities you\'re considering.')
    return " ".join(parts)


async def intake(conversation: list[dict], context, hooks) -> tuple[dict | None, str | None]:
    """(brief, None) to research, or (None, clarifying question)."""
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content'] if m['role'] == 'user' else m['content'][:600]}" for m in conversation)
    result = await Runner.run(intake_agent, transcript, context=context, hooks=hooks, max_turns=1)
    x: Extracted = result.final_output
    if x.off_topic and not x.cities:
        return None, INTRO
    brief, missing, unknown = _resolve(x)
    # Only uncovered cities named: treated like no city named. With a complete brief the Lead picks
    # comparable covered cities and the Writer says the named ones aren't covered; otherwise ask.
    if missing:
        return None, _question(missing, unknown, brief["cities"])
    return brief, None
