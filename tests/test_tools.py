"""Tests for the Researcher tools, against the real data/nomad.duckdb.

    uv run pytest                  # everything
    uv run pytest -m "not network" # skip the live API calls (Open-Meteo, State Dept)
"""

import gzip
import csv
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import os

from tools.advisory import travel_advisory
from tools.coworking import nearby_coworking
from tools.db import DB_PATH, city_info, query_listings, rows
from tools.fx import fx_to_usd
from tools.overlap import work_overlap
from tools.stays import find_stays, neighbourhood_speed
from tools.weather import month_climate

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason="data/nomad.duckdb not built")


def ok(result: str) -> dict:
    data = json.loads(result)
    assert "error" not in data, data["error"]
    assert data.get("citations") or data.get("rows") is not None, "every result should cite its source"
    return data


def err(result: str) -> str:
    data = json.loads(result)
    assert "error" in data, f"expected an error, got {result[:300]}"
    return data["error"]


# --- city and neighbourhood lookup ---


@pytest.mark.parametrize("name", ["Mexico City", "mexico city", "MEXICO-CITY", "mexico-city"])
def test_city_names(name):
    assert city_info(name)["city"] == "Mexico City"


def test_uncovered_city_lists_covered_ones():
    assert "Lisbon" in err(find_stays("Medellín", "2027-02-01", "2027-03-01"))


@pytest.mark.parametrize("city, typed, expected", [
    ("Lisbon", "belem", "Belém"),                        # source dropped the accent; we restored it
    ("Lisbon", "Parque das Nacoes", "Parque das Nações"),
    ("Athens", "Plaka", "Commercial Triangle-Plaka"),    # English name for Greek source, partial match
    ("Athens", "ΚΟΛΩΝΑΚΙ", "Kolonaki"),                  # original script still works
    ("Taipei", "da'an district", "Da'an District"),
    ("Mexico City", "cuauhtemoc", "Cuauhtémoc"),
])
def test_neighbourhood_names(city, typed, expected):
    data = ok(neighbourhood_speed(city, typed))
    assert data["neighbourhoods"][0]["neighbourhood"] == expected


def test_neighbourhood_group_filters_to_city_proper():
    data = ok(find_stays("Lisbon", "2027-02-01", "2027-02-15", neighbourhood="Lisboa"))
    assert data["by_neighbourhood"] and all(r["neighbourhood_group"] == "Lisboa" for r in data["by_neighbourhood"])


def test_unknown_neighbourhood_lists_options():
    message = err(find_stays("Mexico City", "2027-02-01", "2027-02-15", neighbourhood="Atlantis"))
    assert "Cuauhtémoc" in message


# --- find_stays ---


LISBON_FEB = dict(city="Lisbon", check_in="2027-02-01", check_out="2027-03-01")


def test_find_stays_budget_and_prices():
    data = ok(find_stays(**LISBON_FEB, max_total_usd=2500))
    assert data["stay"]["nights"] == 28
    assert 0 < data["n_within_budget"] <= data["n_available"]
    for ex in data["examples"]:
        assert ex["total_usd"] <= 2500
        assert abs(ex["total_usd"] - ex["nightly_usd"] * 28) <= 28  # both rounded
    assert {c["id"] for c in data["citations"]} >= {"airbnb-lisbon", "ookla-fixed-2026-Q3", "fx-EUR"}


def test_find_stays_filters():
    data = ok(find_stays(**LISBON_FEB, needs_workspace=True, room_type="Entire home/apt", min_bedrooms=1))
    assert data["examples"]
    for ex in data["examples"]:
        assert ex["has_workspace"] and ex["room_type"] == "Entire home/apt" and ex["bedrooms"] >= 1


def test_examples_are_an_affordable_and_a_typical_listing_per_neighbourhood():
    data = ok(find_stays("Mexico City", "2027-03-01", "2027-03-29", max_total_usd=1500))
    by_nb: dict[str, dict] = {}
    for ex in data["examples"]:
        assert ex["kind"] not in by_nb.setdefault(ex["neighbourhood"], {})
        by_nb[ex["neighbourhood"]][ex["kind"]] = ex
    assert 1 < len(by_nb) <= 8
    top = data["by_neighbourhood"][0]
    pair = by_nb[top["neighbourhood"]]
    a, t = pair["affordable"], pair["typical"]
    assert a["total_usd"] < t["total_usd"] <= 1500
    assert t["neighbourhood_median_usd"] == top["median_total_usd"]
    assert abs(t["total_usd"] - top["median_total_usd"]) < 0.25 * top["median_total_usd"]
    assert a["room_type"] == t["room_type"]  # not a dorm bed next to an apartment


def test_shared_rooms_included_and_filterable():
    cheap = ok(find_stays("Istanbul", "2027-01-10", "2027-01-24", 300))  # budget travelers: dorms and rooms count
    assert cheap["n_within_budget"] > 0
    shared = ok(find_stays("Mexico City", "2027-01-10", "2027-01-24", 1500, room_type="Shared room"))
    assert shared["examples"] and all(ex["room_type"] == "Shared room" for ex in shared["examples"])


def test_bedroom_cap_defaults_to_two_and_can_be_lifted():
    capped = ok(find_stays(**LISBON_FEB))
    assert all((ex["bedrooms"] or 0) <= 2 for ex in capped["examples"])
    uncapped = ok(find_stays(**LISBON_FEB, max_bedrooms=None))
    assert uncapped["n_available"] > capped["n_available"]
    big = ok(find_stays(**LISBON_FEB, min_bedrooms=4, max_bedrooms=None))
    assert big["examples"] and all(ex["bedrooms"] >= 4 for ex in big["examples"])


def test_a_tighter_budget_never_finds_more():
    loose = ok(find_stays(**LISBON_FEB, max_total_usd=5000))["n_within_budget"]
    tight = ok(find_stays(**LISBON_FEB, max_total_usd=2000))["n_within_budget"]
    assert tight <= loose


@pytest.mark.parametrize("check_in, check_out, needle", [
    ("2027-02-01", "2027-02-04", "at least 7 nights"),
    ("2027-11-01", "2027-12-01", "outside"),
    ("2026-01-01", "2026-02-01", "outside"),
    ("Feb 1", "2027-03-01", "Invalid isoformat"),
])
def test_find_stays_bad_dates(check_in, check_out, needle):
    assert needle in err(find_stays("Lisbon", check_in, check_out))


def _raw_calendar(slug: str, listing_ids: set[int]) -> dict[int, dict[date, tuple[bool, int]]]:
    manifest = json.loads((ROOT / "data/raw/airbnb/manifest.json").read_text())
    path = ROOT / "data/raw/airbnb" / slug / manifest[slug]["snapshot_date"] / "calendar.csv.gz"
    days: dict[int, dict] = {i: {} for i in listing_ids}
    with gzip.open(path, "rt") as f:
        for row in csv.DictReader(f):
            lid = int(row["listing_id"])
            if lid in days:
                days[lid][date.fromisoformat(row["date"])] = (row["available"] == "t", int(row["minimum_nights"]))
    return days


@pytest.mark.skipif(not (ROOT / "data/raw/airbnb/manifest.json").exists(), reason="raw calendars not downloaded")
@pytest.mark.parametrize("city, slug, check_in, nights", [
    ("Lisbon", "lisbon", date(2027, 2, 1), 28),
    ("Taipei", "taipei", date(2027, 4, 12), 9),
])
def test_found_stays_are_open_every_night_in_the_raw_calendar(city, slug, check_in, nights):
    """The open-stretch table must agree with Inside Airbnb's daily calendar."""
    check_out = check_in + timedelta(days=nights)
    data = ok(find_stays(city, check_in.isoformat(), check_out.isoformat()))
    ids = {ex["listing_id"] for ex in data["examples"]}
    assert ids
    raw = _raw_calendar(slug, ids)
    for lid in ids:
        for i in range(nights):
            night = check_in + timedelta(days=i)
            available, min_nights = raw[lid][night]
            assert available, f"listing {lid} isn't open on {night}"
        assert raw[lid][check_in][1] <= nights, f"listing {lid} needs a longer stay"


# --- neighbourhood_speed ---


def test_neighbourhood_speed_city_and_widened():
    city = ok(neighbourhood_speed("Berlin"))
    assert len(city["neighbourhoods"]) > 5 and "widened_3x3" not in city
    one = ok(neighbourhood_speed("Berlin", city["neighbourhoods"][0]["neighbourhood"]))
    assert one["widened_3x3"]["tests"] > 0 and one["widened_3x3"]["tests_weighted_mean_down_mbps_fixed"] > 0


# --- query_listings ---


def test_sql_select_works_and_cites():
    data = ok(query_listings("select neighbourhood, count(*) n from listings where city = 'Porto' group by 1"))
    assert data["rows"] and data["citations"][0]["id"] == "airbnb-porto"


def test_sql_row_limit():
    data = ok(query_listings("select listing_id from listings"))
    assert len(data["rows"]) == 50 and data["truncated"]


@pytest.mark.parametrize("sql", [
    "drop table listings",
    "delete from listings",
    "select 1; drop table listings",
    "attach '/tmp/x.db'",
    "copy listings to '/tmp/out.csv'",
])
def test_sql_rejects_anything_but_one_select(sql):
    err(query_listings(sql))


@pytest.mark.parametrize("sql", [
    "select * from read_csv('/etc/passwd')",
    "select * from read_parquet('https://example.com/x.parquet')",
])
def test_sql_cannot_reach_files_or_network(sql):
    assert "disabled" in err(query_listings(sql)).lower()


def test_sql_error_is_returned_not_raised():
    assert "tables" in json.loads(query_listings("select nope from listings"))


# --- work_overlap ---


@pytest.mark.parametrize("city, home, on, local, outside", [
    ("Lisbon", "Eastern", "2027-02-01", "14:00-22:00", 0),
    ("Lisbon", "Eastern", "2027-03-20", "13:00-21:00", 0),  # US on DST, Portugal not yet
    ("Bangkok", "America/New_York", "2027-02-01", "21:00-05:00 (+1 day)", 7),
    ("Mexico City", "Pacific", "2027-02-01", "11:00-19:00", 0),
])
def test_work_overlap(city, home, on, local, outside):
    data = ok(work_overlap(city, home, "9am", "17:00", on))
    assert data["work_hours_local"] == local
    assert data["hours_outside_8am_10pm_local"] == outside


def test_work_overlap_bad_zone():
    assert "IANA" in err(work_overlap("Lisbon", "Mars/Olympus", "09:00", "17:00"))


# --- fx ---


def test_fx_by_city_and_code():
    assert ok(fx_to_usd("Lisbon"))["currency"] == "EUR"
    ars = ok(fx_to_usd("ARS"))
    assert ars["notes"] and ars["citations"][0]["source"].startswith("ExchangeRate-API")
    assert ok(fx_to_usd("TWD"))["usd_per_unit"] > 0


# --- climate (precomputed by scripts/build_climate.py: no live call) ---


def test_month_climate_lisbon_february():
    data = ok(month_climate("Lisbon", "Feb"))
    assert 12 < data["avg_high_c"] < 20 and 5 < data["avg_low_c"] < 13
    assert 0 < data["rainy_days_per_month"] < 20


def test_month_climate_southern_hemisphere_is_warm_in_january():
    assert ok(month_climate("Buenos Aires", 1))["avg_high_c"] > 25


def test_bad_month():
    assert "isn't a month" in err(month_climate("Lisbon", "Smarch"))


def test_climate_covers_every_city_and_month():
    n = rows("select count(*) n from climate c join cities using (city)")[0]["n"]
    assert n == 12 * rows("select count(*) n from cities")[0]["n"]


def test_month_climate_makes_no_web_request(monkeypatch):
    import httpx

    def no_network(*a, **kw):
        raise AssertionError("month_climate called the network")
    monkeypatch.setattr(httpx, "get", no_network)
    assert ok(month_climate("Tokyo", "August"))["avg_high_c"] > 25


# --- live APIs ---


@pytest.mark.network
@pytest.mark.parametrize("city", ["Santiago", "Prague", "Tokyo", "Lisbon", "Cape Town"])
def test_travel_advisory(city):
    data = ok(travel_advisory(city))
    assert data["level"] in (1, 2, 3, 4)
    assert data["link"].startswith("https://travel.state.gov")
    assert "&nbsp;" not in data["summary_start"]


# --- coworking (Google Places, live: each run uses ~3 requests of the daily quota) ---


def test_coworking_without_key_says_unknown(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    assert "unknown" in err(nearby_coworking("Lisbon", "Arroios"))


def test_coworking_bad_neighbourhood_fails_before_calling_google(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "not-a-real-key")
    assert "No neighbourhood" in err(nearby_coworking("Lisbon", "Atlantis"))


needs_places_key = pytest.mark.skipif(not os.environ.get("GOOGLE_PLACES_API_KEY"), reason="no Places key")


@pytest.mark.network
@needs_places_key
@pytest.mark.parametrize("city, neighbourhood", [("Mexico City", "Cuauhtémoc"), ("Lisbon", "Arroios")])
def test_nearby_coworking(city, neighbourhood):
    data = ok(nearby_coworking(city, neighbourhood))
    assert data["n_coworking"] > 0
    assert data["places"][0]["is_coworking"]  # coworking sorts first
    assert all(p["distance_km"] is None or p["distance_km"] < 3 for p in data["places"])
    assert data["citations"][0]["source"] == "Google Maps (Places API)"


@pytest.mark.network
@needs_places_key
def test_coworking_city_wide():
    assert ok(nearby_coworking("Taipei"))["neighbourhood"] is None
