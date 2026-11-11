"""Monthly climate averages for every city, from Open-Meteo's historical archive, into the DB.

Run after build_db.py (it reads each city's center point from the `cities` table):

    uv run scripts/build_climate.py

Fetches 5 complete years of daily weather per city, once, and writes 12 monthly averages per city
to data/processed/climate.parquet and the `climate` table. build_db.py reloads the parquet on a
rebuild, so the API is only called again for cities whose center or years changed.

Rate limits: one request for 5 years of daily data costs ~65 of the free tier's 600 calls per
minute (Open-Meteo weights requests by two-week chunks), so requests are spaced 7 s apart.
"""

import time
from datetime import date
from pathlib import Path

import duckdb
import httpx

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.weather import YEARS, fetch_daily, monthly_normals  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "nomad.duckdb"
OUT = ROOT / "data" / "processed" / "climate.parquet"
SPACING_S = 7
RETRIES = 3

COLUMNS = """city varchar, month integer, years varchar, lat double, lon double, avg_high_c double,
    avg_low_c double, rainy_days_per_month double, avg_precip_mm_per_month double,
    avg_sunshine_hours_per_day double, avg_daylight_hours_per_day double, retrieved_on varchar"""


def fetch(lat: float, lon: float, first: int, last: int) -> dict:
    for attempt in range(1, RETRIES + 1):
        r = fetch_daily(lat, lon, first, last)
        if r.status_code == 200:
            return r.json()["daily"]
        reason = r.text[:200]
        if r.status_code == 429 and attempt < RETRIES:
            print(f"    429 ({reason}); waiting 65 s")
            time.sleep(65)
            continue
        raise RuntimeError(f"Open-Meteo {r.status_code}: {reason}")
    raise AssertionError("unreachable")


def main() -> None:
    last = date.today().year - 1
    first = last - YEARS + 1
    years = f"{first}-{last}"

    con = duckdb.connect(str(DB))
    cities = con.execute("select city, round(center_lat, 3), round(center_lon, 3) from cities order by city").fetchall()
    con.execute(f"create or replace temp table new_climate({COLUMNS})")
    # Reuse what's already fetched for the same point and years
    if OUT.exists():
        con.execute(f"insert into new_climate select * from '{OUT}' where years = '{years}'")
    have = {(c, round(la, 3), round(lo, 3)) for c, la, lo in
            con.execute("select distinct city, lat, lon from new_climate").fetchall()}
    # Drop cached rows whose city is gone or whose center point moved (they'll be refetched)
    con.execute("delete from new_climate where city not in (select city from cities)")
    for city, lat, lon in cities:
        con.execute("delete from new_climate where city = ? and (round(lat, 3) != ? or round(lon, 3) != ?)",
                    [city, lat, lon])

    todo = [(c, la, lo) for c, la, lo in cities if (c, la, lo) not in have]
    print(f"{len(cities) - len(todo)} cities cached, {len(todo)} to fetch ({years})")
    for i, (city, lat, lon) in enumerate(todo):
        if i:
            time.sleep(SPACING_S)
        print(f"  {city} ({lat}, {lon})")
        normals = monthly_normals(fetch(lat, lon, first, last), YEARS)
        assert len(normals) == 12 and all(n["avg_high_c"] is not None for n in normals), f"{city}: missing months"
        con.executemany("insert into new_climate values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            (city, n["month"], years, lat, lon, n["avg_high_c"], n["avg_low_c"], n["rainy_days_per_month"],
             n["avg_precip_mm_per_month"], n["avg_sunshine_hours_per_day"], n["avg_daylight_hours_per_day"],
             date.today().isoformat()) for n in normals])
        # Save after each city, so an interrupted run keeps what it fetched
        con.execute(f"copy (select * from new_climate order by city, month) to '{OUT}' (format parquet)")

    n = con.execute("select count(*) from new_climate").fetchone()[0]
    assert n == 12 * len(cities), f"expected {12 * len(cities)} rows, got {n}"
    con.execute(f"copy (select * from new_climate order by city, month) to '{OUT}' (format parquet)")
    con.execute("create or replace table climate as select * from new_climate order by city, month")
    con.close()
    print(f"climate: {n} rows -> {OUT.name} and the climate table")


if __name__ == "__main__":
    main()
