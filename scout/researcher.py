"""City Researcher: researches one city for one brief, in a fresh context, and returns structured
findings where every number carries the citation ids of the tool results it came from."""

from typing import Literal

from agents import Agent, RunContextWrapper, function_tool
from pydantic import BaseModel, Field

from scout.config import MAX_COWORKING, MAX_QUERIES_PER_RESEARCHER, RESEARCHER_MODEL, SETTINGS
from scout.state import Research
from tools import advisory, coworking, db, fx, overlap, stays, weather

# --- Tools, as the model sees them. Thin wrappers: the work and the citations are in tools/. ---


@function_tool(strict_mode=False)
def find_stays(
    city: str,
    check_in: str,
    check_out: str,
    max_total_usd: float | None = None,
    needs_workspace: bool = False,
    room_type: str | None = None,
    min_bedrooms: int | None = None,
    max_bedrooms: int | None = 2,
    guests: int = 1,
    neighbourhood: str | None = None,
) -> str:
    """Airbnb listings open for every night of a stay, priced in USD for the whole stay, grouped by
    neighbourhood with measured home-internet speed, plus 5 example listings. Returns counts within
    budget and overall, and p10/median/cheapest totals.

    Args:
        city: A covered city, e.g. 'Lisbon'.
        check_in: Arrival date, YYYY-MM-DD.
        check_out: Departure date, YYYY-MM-DD (the morning you leave).
        max_total_usd: Budget for the whole stay in USD. Omit for no price filter.
        needs_workspace: Only listings with a dedicated workspace.
        room_type: 'Entire home/apt', 'Private room', 'Shared room' or 'Hotel room'. Omit for all.
        min_bedrooms: At least this many bedrooms.
        max_bedrooms: At most this many bedrooms (default 2; null for no cap).
        guests: People staying.
        neighbourhood: One neighbourhood, or a municipality/district group (e.g. 'Lisboa' for Lisbon proper).
    """
    return stays.find_stays(city, check_in, check_out, max_total_usd, needs_workspace, room_type,
                            min_bedrooms, max_bedrooms, guests, neighbourhood)


@function_tool
def neighbourhood_speed(city: str, neighbourhood: str | None = None) -> str:
    """Measured internet speed (Ookla fixed and mobile) around a city's listings, per neighbourhood. For
    one neighbourhood, also a widened 3x3-tile estimate: use it when share_low_confidence is high.

    Args:
        city: A covered city.
        neighbourhood: One neighbourhood or group. Omit for the city's largest neighbourhoods.
    """
    return stays.neighbourhood_speed(city, neighbourhood)


@function_tool
def month_climate(city: str, month: str) -> str:
    """Typical weather for a month: average high/low, rainy days, sunshine and daylight, 2021-2025.

    Args:
        city: A covered city.
        month: Month name or number, e.g. 'February' or '2'.
    """
    return weather.month_climate(city, month)


@function_tool
def travel_advisory(city: str) -> str:
    """The U.S. State Department travel advisory level (1-4) and link for the city's country.

    Args:
        city: A covered city.
    """
    return advisory.travel_advisory(city)


@function_tool
def fx_to_usd(city_or_currency: str) -> str:
    """Exchange rate to USD for a currency code or a covered city's currency.

    Args:
        city_or_currency: e.g. 'EUR' or 'Buenos Aires'.
    """
    return fx.fx_to_usd(city_or_currency)


@function_tool(strict_mode=False)
def work_overlap(city: str, home_tz: str, work_start: str, work_end: str, on_date: str | None = None) -> str:
    """Where the user's home-time-zone work hours fall in the city's local time, and how many of those
    hours are outside 08:00-22:00 local.

    Args:
        city: A covered city.
        home_tz: The user's home time zone, e.g. 'America/New_York' or 'Eastern'.
        work_start: Start of required online hours in home time, e.g. '09:00'.
        work_end: End of required online hours in home time, e.g. '17:00'.
        on_date: A date during the stay (YYYY-MM-DD), so daylight saving time is right.
    """
    return overlap.work_overlap(city, home_tz, work_start, work_end, on_date)


@function_tool(strict_mode=False)
def nearby_coworking(ctx: RunContextWrapper[Research], city: str, neighbourhood: str | None = None) -> str:
    """Coworking spaces within ~1.5 km of a neighbourhood's listings (Google Maps): ratings, hours,
    websites. Use only when the stay has no dedicated workspace (e.g. a shared room).

    Args:
        city: A covered city.
        neighbourhood: The neighbourhood you're recommending.
    """
    if ctx.context.coworking_calls >= MAX_COWORKING:
        return db.error(f"Coworking search limit ({MAX_COWORKING} per question) reached. Report coworking as not checked.")
    ctx.context.coworking_calls += 1
    return coworking.nearby_coworking(city, neighbourhood)


@function_tool
def query_listings(ctx: RunContextWrapper[Research], sql: str) -> str:
    """Read-only SQL (DuckDB) for questions the other tools don't cover. One SELECT, max 50 rows.
    Tables: listings(listing_id, city, neighbourhood, neighbourhood_group, room_type, bedrooms,
    accommodates, price_local, minimum_nights, maximum_nights, has_workspace, has_wifi,
    wifi_mbps_listed, rating, n_reviews, quadkey, lat, lon), availability(listing_id, city, start_date,
    end_date, nights, min_nights_max) = unbroken open stretches, speed_tiles(city, type, quadkey,
    avg_d_mbps, avg_u_mbps, avg_lat_ms, tests), cities, fx_rates(currency, usd_per_unit),
    neighbourhood_summary. price_local is nightly, in the city's currency.

    Args:
        sql: One SELECT statement. Always filter by city.
    """
    # Per researcher: in testing one spent its whole turn budget on custom queries and returned nothing
    agent = getattr(getattr(ctx, "agent", None), "name", "?")
    used = ctx.context.queries_by_agent.get(agent, 0)
    if used >= MAX_QUERIES_PER_RESEARCHER:
        return db.error(f"Custom query limit ({MAX_QUERIES_PER_RESEARCHER}) reached. Return your findings now.")
    ctx.context.queries_by_agent[agent] = used + 1
    return db.query_listings(sql)


RESEARCHER_TOOLS = [find_stays, neighbourhood_speed, month_climate, travel_advisory, fx_to_usd,
                    work_overlap, nearby_coworking, query_listings]

# --- What a Researcher returns ---


class Fact(BaseModel):
    claim: str = Field(description="One specific sentence with the number(s), e.g. 'Median 28-night total in Arroios: $2,149.'")
    cites: list[str] = Field(description="Citation ids from the tool results this claim came from.")


class NeighbourhoodFinding(BaseModel):
    name: str
    n_within_budget: int | None
    median_total_usd: float | None
    median_down_mbps_fixed: float | None
    low_confidence_speed: bool = Field(description="True if many listings sit in tiles with few speed tests.")
    coworking_nearby: int | None = Field(description="Coworking spaces found nearby, if searched; null if not searched.")
    why: str = Field(description="One line: why this neighbourhood suits (or doesn't suit) the brief.")
    example_listing_urls: list[str]
    cites: list[str]


class CityFindings(BaseModel):
    city: str
    fit: Literal["good", "tight", "poor", "unknown"] = Field(
        description="Overall fit to the brief: budget, availability, work-hours overlap, internet.")
    summary: str = Field(description="Two or three sentences: the bottom line for this city.")
    n_available: int | None = Field(description="Listings open for the stay (any price).")
    n_within_budget: int | None
    median_total_usd: float | None = Field(description="Median stay total among available listings.")
    work_hours_local: str | None
    hours_outside_8am_10pm: float | None
    weather: str | None = Field(description="In °F (avg_high_f / avg_low_f), e.g. 'Feb avg high 61.9°F / low 49.6°F, 8.6 rainy days'")
    advisory_level: int | None
    advisory_url: str | None
    neighbourhoods: list[NeighbourhoodFinding] = Field(description="1-3 best neighbourhoods for the brief.")
    facts: list[Fact] = Field(description="Every other number worth reporting, each with its citations.")
    gaps: list[str] = Field(description="What's scarce, low-confidence, failed, or couldn't be checked.")


researcher = Agent(
    name="Researcher",
    instructions="""\
You research ONE city for a remote worker's stay, using only your tools. You get the trip brief and a
task from the lead.

For a first look at a city (unless the task says otherwise):
1. find_stays with the brief's dates, budget (max_total_usd = budget_total_usd), needs_workspace, room_type,
   min_bedrooms and guests. If budget_total_usd is null, omit max_total_usd. Read room_type in the results: a cheap
   listing may be a private or shared room, not an apartment.
   - For Lisbon or Porto, also try neighbourhood='Lisboa' / 'PORTO' for the city proper, since their data
     covers the whole metro area.
2. Pick the 1-3 neighbourhoods that best fit: enough listings within budget, fast fixed internet, low
   share_low_confidence. If a pick has share_low_confidence above 0.3, call neighbourhood_speed for it.
3. month_climate for the month of check_in. travel_advisory for the city.
4. If the brief has work hours and a home time zone, work_overlap with a date during the stay.
5. If the brief has wants_coworking, or the stay has no dedicated workspace (room_type 'Shared room', or
   most good listings lack one), call nearby_coworking for your top 1-2 neighbourhoods.
For a follow-up task, do just what it asks.

Rules:
- Every number you report must come from a tool result, copied exactly, with that result's citation ids
  in `cites`. For a neighbourhood, n_within_budget is the n_listings in find_stays' by_neighbourhood row.
- Give 2 example_listing_urls per neighbourhood from find_stays' examples: its 'affordable' one, then its
  'typical' one. If none are in that neighbourhood, call find_stays again with neighbourhood set to it.
- If a tool returns an error, try once more with fixed arguments; otherwise record it in `gaps`.
- query_listings is only for what the other tools can't answer, at most 3 calls. Once you have the steps
  above, return your findings: you have a limited number of turns.
- Under 5 listings within budget, low-confidence speeds, a failed tool: say so in `gaps`.
- Don't add facts from your own knowledge (no visa rules, safety opinions, or neighbourhood vibes).""",
    model=RESEARCHER_MODEL,
    model_settings=SETTINGS,
    tools=RESEARCHER_TOOLS,
    output_type=CityFindings,
)
