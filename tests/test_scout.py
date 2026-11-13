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
                guests=1, preferences=[], wants_coworking=False, other_questions=[], off_topic=False,
                latest_is_chitchat=False, about_previous_answer=False, rank_neighbourhoods_by=None)
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


def test_reconcile_links_the_affordable_then_typical_example_and_the_writer_gets_their_prices():
    from scout.summary import listing_examples

    s = _state_with_find_stays([PALERMO])
    url = "https://www.airbnb.com/rooms/"
    s.tool_calls[0]["result"] = json.dumps({
        "stay": {"nights": 28}, "by_neighbourhood": [PALERMO],
        "citations": [{"id": "airbnb-buenos-aires"}, {"id": "ookla-fixed-2026-Q3"}],
        "examples": [{"kind": "typical", "neighbourhood": "Palermo", "listing_url": url + "2", "total_usd": 1889.0,
                      "room_type": "Entire home/apt"},
                     {"kind": "affordable", "neighbourhood": "Palermo", "listing_url": url + "1", "total_usd": 990.0,
                      "room_type": "Entire home/apt"}]})
    nb = NeighbourhoodFinding(name="Palermo", n_within_budget=1799, median_total_usd=1889.0,
                              median_down_mbps_fixed=273.0, low_confidence_speed=False, coworking_nearby=None,
                              why="", example_listing_urls=[url + "9"], cites=[])
    findings = CityFindings(city="Buenos Aires", fit="good", summary="", n_available=1, n_within_budget=1,
                            median_total_usd=1, work_hours_local=None, hours_outside_8am_10pm=None, weather=None,
                            advisory_level=None, advisory_url=None, neighbourhoods=[nb], facts=[], gaps=[])
    reconcile(findings, s, "Researcher-Buenos Aires")
    assert nb.example_listing_urls == [url + "1", url + "2"]
    s.findings["Researcher-Buenos Aires"] = findings
    ex = listing_examples(s)
    assert ex[url + "1"]["kind"] == "affordable" and ex[url + "1"]["total_usd"] == 990.0
    assert ex[url + "2"]["cites"] == ["airbnb-buenos-aires"] and ex[url + "2"]["nights"] == 28


# --- the page's city cards: numbers come from tool results, not retyped findings ---


def test_a_thanks_after_an_answer_gets_a_short_reply_not_new_research(monkeypatch):
    import scout.intake as intake_mod

    full = dict(cities=["Lisbon"], month=2, nights=28, budget_amount=2500, budget_period="stay",
                home_tz="America/New_York", work_start="09:00", work_end="17:00")

    async def fake_run(agent, transcript, **kw):
        return SimpleNamespace(final_output=extracted(**full, latest_is_chitchat=True))

    monkeypatch.setattr(intake_mod.Runner, "run", fake_run)
    after_answer = [{"role": "user", "content": "Lisbon in Feb..."}, {"role": "assistant", "content": "## Lisbon..."},
                    {"role": "user", "content": "thanks!"}]
    brief, reply = asyncio.run(intake_mod.intake(after_answer, None, None))
    assert brief is None and reply == intake_mod.CHITCHAT_REPLY
    # A first message is never treated as small talk about an answer
    brief, reply = asyncio.run(intake_mod.intake(after_answer[:1], None, None))
    assert brief and brief["cities"] == ["Lisbon"]


REPORT = """**Bottom line**: Buenos Aires first.

## Buenos Aires: best
| Neighbourhood | Listings in budget | Median download (fixed) |
| --- | --- | --- |
| Palermo | 1,398 [airbnb-buenos-aires] | 271 Mbps [ookla-fixed-2026-Q3] |

## What this data can't tell you
Visas.

## Sources
- **[airbnb-buenos-aires]** Inside Airbnb: Buenos Aires, snapshot 2026-09-20, listings + availability <https://insideairbnb.com/get-the-data/>
- **[ookla-fixed-2026-Q3]** Speedtest® by Ookla® Global Fixed and Mobile Network Performance Maps: fixed tiles, 2026-Q3"""


def test_followup_helpers_find_the_report_its_sources_and_unsupported_numbers():
    from scout.followup import last_report, report_sources, unsupported_numbers

    conv = [{"role": "user", "content": "q"}, {"role": "assistant", "content": REPORT},
            {"role": "user", "content": "which is fastest?"},
            {"role": "assistant", "content": "Palermo, 271 Mbps.\n\n## Sources\n- **[ookla-fixed-2026-Q3]** x: y"},
            {"role": "user", "content": "and the cheapest?"}]
    assert last_report(conv) == 1  # the full report, not the quick answer after it
    src = report_sources(REPORT)
    assert src["airbnb-buenos-aires"] == {"source": "Inside Airbnb", "url": "https://insideairbnb.com/get-the-data/",
                                          "detail": "Buenos Aires, snapshot 2026-09-20, listings + availability"}
    assert src["ookla-fixed-2026-Q3"]["url"] is None
    assert unsupported_numbers("Palermo has 1,398 stays at 271 Mbps [ookla-fixed-2026-Q3].", REPORT) == []
    assert unsupported_numbers("Palermo averages 300 Mbps.", REPORT) == [300.0]


def test_a_question_about_the_last_answer_skips_research(monkeypatch):
    import scout.pipeline as pipeline

    async def fake_intake(conversation, state, hooks):
        return {"cities": ["Buenos Aires"], "about_previous_answer": True}, None

    async def fake_answer(conversation, state):
        return "Palermo: 271 Mbps [ookla-fixed-2026-Q3]."

    async def no_research(*a, **kw):
        raise AssertionError("research should not run")

    monkeypatch.setattr(pipeline, "intake", fake_intake)
    monkeypatch.setattr(pipeline, "answer_from_report", fake_answer)
    monkeypatch.setattr(pipeline.Runner, "run", no_research)
    text, state = asyncio.run(pipeline.answer([{"role": "user", "content": "which is fastest?"}]))
    assert text.startswith("Palermo") and state.followup and "about_previous_answer" not in state.brief


def test_city_cards_use_the_matching_city_wide_call():
    from scout.summary import city_cards

    s = Research()
    s.brief = {"budget_total_usd": 2500, "room_type": "Entire home/apt", "needs_workspace": True}
    cite = lambda i: [{"id": i, "source": "x", "detail": "y"}]  # noqa: E731

    def call(name, args, result):
        s.tool_calls.append({"agent": "Researcher-Lisbon", "name": name, "args": args, "result": json.dumps(result)})

    full = {"city": "Lisbon", "max_total_usd": 2500, "room_type": "Entire home/apt", "needs_workspace": True}
    call("find_stays", full, {"n_available": 900, "n_within_budget": 120, "median_total_usd": 3100, "p10_total_usd": 1500,
                              "stay": {"nights": 28}, "citations": cite("airbnb-lisbon") + cite("ookla-fixed-2026-Q3")})
    call("find_stays", {**full, "neighbourhood": "Arroios"}, {"n_available": 50, "n_within_budget": 9})   # narrower
    call("find_stays", {"city": "Lisbon"}, {"n_available": 8000, "median_total_usd": 2900})               # unfiltered
    call("work_overlap", {"city": "Lisbon"}, {"work_hours_local": "14:00-22:00", "hours_outside_8am_10pm_local": 0,
                                              "citations": cite("iana-tz")})
    call("travel_advisory", {"city": "Lisbon"}, {"level": 1, "link": "https://t", "citations": cite("state-dept-pt")})
    nb = lambda name, mbps: NeighbourhoodFinding(  # noqa: E731
        name=name, n_within_budget=1, median_total_usd=1, median_down_mbps_fixed=mbps, low_confidence_speed=False,
        coworking_nearby=None, why="", example_listing_urls=[], cites=[])
    s.findings["Researcher-Lisbon"] = CityFindings(
        city="Lisbon", fit="tight", summary="", n_available=1, n_within_budget=1, median_total_usd=1,
        work_hours_local=None, hours_outside_8am_10pm=None, weather=None, advisory_level=None, advisory_url=None,
        neighbourhoods=[nb("Arroios", 286.0), nb("Estrela", 336.0)], facts=[], gaps=[])

    [card] = city_cards(s)
    assert card["fit"] == "tight"
    assert card["stays"]["n_within_budget"] == 120 and card["stays"]["median_total_usd"] == 3100
    assert card["stays"]["cites"] == ["airbnb-lisbon"] and card["speed"]["cites"] == ["ookla-fixed-2026-Q3"]
    assert (card["speed"]["min"], card["speed"]["max"]) == (286.0, 336.0)
    assert card["hours"]["local"] == "14:00-22:00" and card["advisory"]["level"] == 1
    assert len(card["climate"]["months"]) == 12
    assert card["climate"]["months"][1]["daylight"] > 0
    assert [n["name"] for n in card["neighbourhoods"]] == ["Arroios", "Estrela"]
    assert card["daylight"] is None  # no stay dates in this brief


def test_sun_times_match_published_values():
    from datetime import date
    from scout.summary import sun_times

    # Lisbon, June 21 2027: sunrise 06:11, sunset 21:05 (timeanddate.com); NOAA's equations are good to ~2 min
    rise, sets = sun_times(38.7223, -9.1393, date(2027, 6, 21), "Europe/Lisbon")
    assert abs((rise.hour * 60 + rise.minute) - (6 * 60 + 11)) <= 3
    assert abs((sets.hour * 60 + sets.minute) - (21 * 60 + 5)) <= 3


@pytest.mark.skipif(not DB_PATH.exists(), reason="database not built")
def test_city_cards_daylight_for_the_stay_is_cited():
    from scout.summary import city_cards

    s = Research()
    s.brief = {"check_in": "2027-04-05", "check_out": "2027-04-19"}
    s.findings["Researcher-Lisbon"] = CityFindings(
        city="Lisbon", fit="good", summary="", n_available=1, n_within_budget=1, median_total_usd=1,
        work_hours_local=None, hours_outside_8am_10pm=None, weather=None, advisory_level=None, advisory_url=None,
        neighbourhoods=[], facts=[], gaps=[])
    [card] = city_cards(s)
    d = card["daylight"]
    assert (d["first"]["date"], d["last"]["date"]) == ("2027-04-05", "2027-04-18")
    assert d["first"]["sunset"] < d["last"]["sunset"]  # days lengthen in April
    assert 12 < d["mid"]["hours"] < 14
    assert d["cites"] == ["solar-lisbon"] and "NOAA" in s.citations["solar-lisbon"]["source"]


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
