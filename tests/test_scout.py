"""Offline tests for the agent pipeline's code paths (no model calls): the clarification gate's
rules, budget arithmetic, citation checks, the Verifier's number check, reconciliation, and the
hooks that build /chat's tool_calls."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from scout.intake import Extracted, _resolve
from scout.lead import reconcile
from scout.researcher import CityFindings, NeighbourhoodFinding
from scout.state import RecordingHooks, Research
from scout.verify import unsupported_sentences
from scout.writer import cited_ids, sources_section
from tools.db import DB_PATH

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="data/nomad.duckdb not built")


def extracted(**kw) -> Extracted:
    base = dict(cities=[], check_in=None, check_out=None, month=None, season=None, year=None, nights=None,
                budget_amount=None, budget_period=None, budget_currency=None, home_tz=None, work_start=None,
                work_end=None, flexible_hours=False, needs_workspace=False, room_type=None, min_bedrooms=None,
                guests=1, preferences=[], wants_coworking=False, other_questions=[], off_topic=False)
    return Extracted(**(base | kw))


# --- the clarification gate ---


def test_vague_request_asks_for_timing_length_budget_and_hours():
    _, missing, _ = _resolve(extracted(preferences=["warm", "cheap"]))
    assert len(missing) == 4


def test_month_without_length_is_ambiguous_without_a_city():
    _, missing, _ = _resolve(extracted(month=2, budget_amount=2000, home_tz="America/New_York", work_start="09:00",
                                       work_end="17:00"))
    assert any("how long" in m for m in missing)


def test_complete_brief_without_city_proceeds():
    brief, missing, _ = _resolve(extracted(month=2, nights=21, budget_amount=1800, budget_period="stay",
                                           flexible_hours=True))
    assert not missing and brief["check_in"].endswith("-02-01") and brief["nights"] == 21


@pytest.mark.parametrize("kw, asks", [
    (dict(nights=182), "when"),         # "Lisbon for 6 months": when?
    (dict(month=3), "how long"),        # "Lisbon in March": for how long?
    (dict(), "when"),                   # "Lisbon": both
])
def test_named_city_still_needs_timing_and_length(kw, asks):
    _, missing, _ = _resolve(extracted(cities=["Lisbon"], **kw))
    assert any(asks in m for m in missing)
    assert not any("budget" in m or "hours" in m for m in missing)


def test_named_city_with_timing_proceeds_without_budget_or_hours():
    brief, missing, _ = _resolve(extracted(cities=["Lisbon"], month=3, nights=182))
    assert not missing
    assert brief["cities"] == ["Lisbon"] and brief["budget_total_usd"] is None
    assert any("no budget" in a for a in brief["assumptions"]) and any("work hours" in a for a in brief["assumptions"])


def test_season_is_enough_timing_and_is_stated():
    brief, missing, _ = _resolve(extracted(cities=["Lisbon"], season="summer", nights=21))
    assert not missing and brief["month"] == "July"
    assert any("summer" in a for a in brief["assumptions"])


@pytest.mark.parametrize("amount, period, nights, total", [
    (2500, "month", 28, 2500),  # "$2,500/month" for a February is $2,500, not 2500 * 28/30
    (2500, "month", 182, 15167),
    (100, "night", 14, 1400),
    (500, "week", 21, 1500),
    (1800, "stay", 21, 1800),
])
def test_budget_conversion(amount, period, nights, total):
    brief, _, _ = _resolve(extracted(cities=["Porto"], month=3, nights=nights, budget_amount=amount, budget_period=period))
    assert brief["budget_total_usd"] == total


def test_one_bedroom_means_a_whole_place():
    brief, _, _ = _resolve(extracted(cities=["Lisbon"], min_bedrooms=1))
    assert brief["room_type"] == "Entire home/apt"


def test_non_usd_budget_asks_for_dollars():
    _, missing, _ = _resolve(extracted(cities=["Lisbon"], budget_amount=2000, budget_currency="EUR"))
    assert any("US dollars" in m for m in missing)


def test_too_short_and_out_of_window_stays_are_refused():
    _, short, _ = _resolve(extracted(cities=["Lisbon"], check_in="2027-02-01", nights=3))
    assert any("at least 7 nights" in m for m in short)
    _, late, _ = _resolve(extracted(cities=["Lisbon"], check_in="2028-02-01", nights=28))
    assert any("covers that window" in m for m in late)


def test_uncovered_city_is_reported_and_complete_brief_proceeds():
    brief, missing, unknown = _resolve(extracted(cities=["Medellín"], month=3, nights=28, budget_amount=1500,
                                                 budget_period="stay", home_tz="America/Los_Angeles",
                                                 work_start="09:00", work_end="17:00"))
    assert unknown == ["Medellín"] and brief["cities"] == [] and not missing


# --- citations ---


def test_cited_ids_skip_links_and_plain_brackets():
    text = "x [airbnb-lisbon, ookla-fixed-2026-Q3] [Level 1](https://travel.state.gov) [note] [fx-EUR]"
    assert cited_ids(text) == ["airbnb-lisbon", "ookla-fixed-2026-Q3", "fx-EUR"]


def test_overlap_citation_id_is_recognised():
    from tools.overlap import work_overlap
    cid = json.loads(work_overlap("Lisbon", "America/New_York", "09:00", "17:00"))["citations"][0]["id"]
    assert cited_ids(f"14:00-22:00 [{cid}]") == [cid]


def test_sources_section_uses_tool_citations():
    out = sources_section(["fx-EUR"], {"fx-EUR": {"source": "Frankfurter", "detail": "1 EUR = 1.1 USD"}})
    assert "**[fx-EUR]** Frankfurter: 1 EUR = 1.1 USD" in out


# --- the Verifier's number check, and reconciliation ---


def _state_with_find_stays(rows: list[dict], agent="Researcher-Buenos Aires", budget=2500) -> Research:
    s = Research()
    s.brief = {"budget_total_usd": budget, "nights": 28}
    s.tool_calls = [{"agent": agent, "name": "find_stays", "args": {"max_total_usd": budget},
                     "result": json.dumps({"by_neighbourhood": rows, "examples": [
                         {"neighbourhood": "Palermo", "listing_url": "https://www.airbnb.com/rooms/1"}]})}]
    return s


PALERMO = {"neighbourhood": "Palermo", "n_listings": 1799, "median_total_usd": 1889.0,
           "median_down_mbps_fixed": 273.0, "share_low_confidence": 0.0}


def test_verifier_flags_a_garbled_count_with_its_table_context():
    s = _state_with_find_stays([PALERMO])
    report = ("## Buenos Aires: best\n| Neighbourhood | Listings in budget | Median stay total |\n| --- | --- | --- |\n"
              "| Palermo | 1,121 [airbnb-buenos-aires] | $1,889 [airbnb-buenos-aires] |\n"
              "The budget is $2,500 for 28 nights.")
    flagged = unsupported_sentences(report, s)
    assert len(flagged) == 1
    assert "Palermo" in flagged[0] and "Buenos Aires" in flagged[0] and "Listings in budget" in flagged[0]


def test_reconcile_fixes_a_garbled_number_and_fills_links():
    s = _state_with_find_stays([PALERMO])
    nb = NeighbourhoodFinding(name="Palermo", n_within_budget=1121, median_total_usd=1889.0,
                              median_down_mbps_fixed=273.0, low_confidence_speed=False, coworking_nearby=None,
                              why="", example_listing_urls=[], cites=[])
    findings = CityFindings(city="Buenos Aires", fit="good", summary="", n_available=1, n_within_budget=1,
                            median_total_usd=1, work_hours_local=None, hours_outside_8am_10pm=None, weather=None,
                            advisory_level=None, advisory_url=None, neighbourhoods=[nb], facts=[], gaps=[])
    reconcile(findings, s, "Researcher-Buenos Aires")
    assert nb.n_within_budget == 1799 and nb.example_listing_urls == ["https://www.airbnb.com/rooms/1"]
    assert s.corrections


def test_reconcile_keeps_a_number_from_another_of_the_researchers_calls():
    """Amsterdam: the researcher reported 1 listing within budget from a narrower call; an unfiltered
    call for the same neighbourhood said 122. The reported number is real: keep it."""
    s = _state_with_find_stays([{**PALERMO, "neighbourhood": "Centrum-Oost", "n_listings": 1, "median_total_usd": 680.0}],
                               agent="Researcher-Amsterdam", budget=1200)
    s.tool_calls.append({"agent": "Researcher-Amsterdam", "name": "find_stays", "args": {},
                         "result": json.dumps({"by_neighbourhood": [{**PALERMO, "neighbourhood": "Centrum-Oost",
                                                                     "n_listings": 122, "median_total_usd": 6355.0}]})})
    nb = NeighbourhoodFinding(name="Centrum-Oost", n_within_budget=1, median_total_usd=680.0,
                              median_down_mbps_fixed=273.0, low_confidence_speed=False, coworking_nearby=None,
                              why="", example_listing_urls=["u"], cites=[])
    findings = CityFindings(city="Amsterdam", fit="poor", summary="", n_available=1, n_within_budget=1,
                            median_total_usd=1, work_hours_local=None, hours_outside_8am_10pm=None, weather=None,
                            advisory_level=None, advisory_url=None, neighbourhoods=[nb], facts=[], gaps=[])
    reconcile(findings, s, "Researcher-Amsterdam")
    assert nb.n_within_budget == 1 and nb.median_total_usd == 680.0 and not s.corrections


# --- hooks: /chat's tool_calls ---


def test_hooks_record_agent_args_result_and_citations():
    s = Research()
    ctx = SimpleNamespace(context=s, tool_arguments='{"city": "Lisbon"}')
    result = json.dumps({"x": 1, "citations": [{"id": "airbnb-lisbon", "source": "Inside Airbnb", "detail": "d"}]})
    asyncio.run(RecordingHooks().on_tool_end(ctx, SimpleNamespace(name="Researcher-Lisbon"),
                                             SimpleNamespace(name="find_stays"), result))
    assert s.tool_calls == [{"agent": "Researcher-Lisbon", "name": "find_stays", "args": {"city": "Lisbon"},
                             "result": result}]
    assert "airbnb-lisbon" in s.citations
