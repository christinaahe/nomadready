"""Build data/nomad.duckdb from the downloaded Inside Airbnb files and the filtered Ookla tiles.

Run download_airbnb.py and download_ookla.py first. Then:

    uv run scripts/build_db.py

Tables
    cities        one row per city: country, time zone, currency, snapshot dates, a center point
    listings      priced listings with at least one open stretch of MIN_STAY+ nights
    availability  one row per open stretch: a listing is free every night from start_date to
                  end_date (inclusive). Built from the daily calendar (~160M rows -> ~1M).
    speed_tiles   Ookla fixed and mobile tiles around each city (zoom 16, ~600 m)
    fx_rates      USD per unit of each city's currency, fetched once at build time
    climate       12 monthly weather averages per city (from build_climate.py's parquet, if present)
    neighbourhood_summary (view)  per-neighbourhood counts, prices, and measured speeds

Listings join speed_tiles on (city, quadkey): the listing's zoom-16 quadkey is computed here
from its lat/lon, so the app needs no geometry at runtime.
"""

import json
from datetime import date
from pathlib import Path

import duckdb
import httpx
import mercantile

from cities import CITY_LIST
from neighbourhood_names import RENAME, ROWS as RENAME_ROWS

ROOT = Path(__file__).resolve().parent.parent
AIRBNB = ROOT / "data" / "raw" / "airbnb"
PROCESSED = ROOT / "data" / "processed"
DB = ROOT / "data" / "nomad.duckdb"

MIN_STAY = 7  # shortest stay the product supports, in nights
LOW_CONFIDENCE_TESTS = 10  # a tile with fewer Ookla tests than this is low confidence

FRANKFURTER = "https://api.frankfurter.app/latest"
ER_API = "https://open.er-api.com/v6/latest/USD"  # for currencies the ECB doesn't publish


def quadkey16(lat: float, lon: float) -> str:
    return mercantile.quadkey(mercantile.tile(lon, lat, 16))


def fetch_fx(currencies: set[str]) -> list[tuple]:
    """(currency, usd_per_unit, source, as_of) for each currency. Frankfurter (ECB) first;
    open.er-api.com for the ones it lacks (ARS, CLP, TWD)."""
    rows = []
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        ecb = client.get(FRANKFURTER, params={"from": "USD"}).json()
        for cur in sorted(currencies):
            if cur in ecb["rates"]:
                rows.append((cur, 1 / ecb["rates"][cur], "Frankfurter (ECB reference rates)", ecb["date"]))
        missing = currencies - {r[0] for r in rows}
        if missing:
            er = client.get(ER_API).json()
            as_of = er["time_last_update_utc"][5:16]  # 'Fri, 06 Nov 2026 00:00:01 +0000' -> '06 Nov 2026'
            for cur in sorted(missing):
                rows.append((cur, 1 / er["rates"][cur], "ExchangeRate-API (open.er-api.com)", as_of))
    if missing := currencies - {r[0] for r in rows}:
        raise RuntimeError(f"No exchange rate for {missing}")
    return rows


def main() -> None:
    manifest = json.loads((AIRBNB / "manifest.json").read_text())
    ookla = json.loads((PROCESSED / "ookla_meta.json").read_text())
    ookla_quarter = f"{ookla['year']}-Q{ookla['quarter']}"

    tmp = DB.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    # A zoom-16 web-mercator quadkey, as Ookla keys its tiles. In SQL so no numpy is needed;
    # checked against mercantile below.
    con.execute("create macro tile_x(lon) as cast(floor((lon + 180) / 360 * 65536) as int)")
    con.execute("create macro tile_y(lat) as cast(floor((1 - ln(tan(radians(lat)) + 1 / cos(radians(lat))) / pi()) / 2 * 65536) as int)")
    con.execute("""create macro quadkey16(lat, lon) as array_to_string(list_transform(range(15, -1, -1),
        lambda i: cast(((tile_x(lon) >> i) & 1) + 2 * ((tile_y(lat) >> i) & 1) as varchar)), '')""")

    con.execute("""
        create table cities(city varchar primary key, slug varchar, country varchar, iso2 varchar,
            timezone varchar, currency varchar, airbnb_snapshot_date date, ookla_quarter varchar,
            center_lat double, center_lon double)""")
    con.execute("""
        create table listings(listing_id bigint primary key, city varchar, neighbourhood varchar,
            neighbourhood_local varchar, neighbourhood_group varchar, name varchar, listing_url varchar, lat double, lon double, room_type varchar,
            accommodates int, bedrooms int, beds int, price_local double, minimum_nights int,
            maximum_nights int, has_workspace boolean, has_wifi boolean, has_ethernet boolean,
            wifi_mbps_listed int, rating double, n_reviews int, last_review date, quadkey varchar)""")
    con.execute("""
        create table availability(listing_id bigint, city varchar, start_date date, end_date date,
            nights int, min_nights_max int)""")
    # Display names for neighbourhoods whose source names are broken or not in Latin script
    con.execute("create temp table rename(city varchar, local varchar, display varchar)")
    con.executemany("insert into rename values (?, ?, ?)", RENAME_ROWS)

    for c in CITY_LIST:
        snap = manifest[c["slug"]]["snapshot_date"]
        folder = AIRBNB / c["slug"] / snap
        print(f"{c['city']:15} {snap} ...", end=" ", flush=True)

        # Open stretches: consecutive available nights. Subtracting a row number from the date
        # gives the same value for every night in one unbroken run (gaps and islands).
        con.execute(f"""
            insert into availability
            select listing_id, ?, min(d), max(d), count(*), max(mn)
            from (
                select cast(listing_id as bigint) listing_id, cast(date as date) d,
                       cast(minimum_nights as int) mn,
                       cast(date as date) - cast(row_number() over (partition by listing_id order by date) as int) grp
                from read_csv('{folder / "calendar.csv.gz"}', header=true, all_varchar=true)
                where available = 't')
            group by listing_id, grp
            having count(*) >= {MIN_STAY}""", [c["city"]])

        # Listings: priced, located, and with at least one open stretch kept above.
        # price is in local currency despite its '$'. Amenities are a JSON array; Airbnb's own
        # wifi speed test shows up as e.g. "Fast wifi – 208 Mbps" (escaped in the file).
        con.execute(f"""
            insert into listings
            select l.listing_id, l.city, coalesce(r.display, l.neighbourhood_local), l.* exclude (listing_id, city),
                   quadkey16(lat, lon)
            from (
                select cast(id as bigint) listing_id, ? city, neighbourhood_cleansed neighbourhood_local,
                       nullif(trim(neighbourhood_group_cleansed), '') neighbourhood_group, name, listing_url,
                       cast(latitude as double) lat, cast(longitude as double) lon, room_type,
                       try_cast(accommodates as int), try_cast(try_cast(bedrooms as double) as int),
                       try_cast(try_cast(beds as double) as int),
                       try_cast(replace(replace(price, '$', ''), ',', '') as double) price_local,
                       try_cast(minimum_nights as int), try_cast(maximum_nights as int),
                       amenities ilike '%dedicated workspace%', amenities ilike '%wifi%',
                       amenities ilike '%ethernet%',
                       try_cast(regexp_extract(amenities, 'ifi \\\\u2013 (\\d+) Mbps', 1) as int),
                       try_cast(review_scores_rating as double), try_cast(number_of_reviews as int),
                       try_cast(last_review as date)
                from read_csv('{folder / "listings.csv.gz"}', header=true, all_varchar=true)
            ) l
            left join rename r on r.city = l.city and r.local = l.neighbourhood_local
            where price_local > 0 and lat is not null and lon is not null
              and exists (select 1 from availability a where a.listing_id = l.listing_id)""", [c["city"]])

        lat, lon, n = con.execute(
            "select median(lat), median(lon), count(*) from listings where city = ?", [c["city"]]).fetchone()
        con.execute("insert into cities values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            c["city"], c["slug"], c["country"], c["iso2"], c["timezone"], c["currency"], snap,
            ookla_quarter, lat, lon])
        print(f"{n:,} listings")

    # Stretches for listings we dropped (no price, no location) have nothing to join to
    con.execute("delete from availability where listing_id not in (select listing_id from listings)")

    con.execute(f"""
        create table speed_tiles as
        select city, type, quadkey, avg_d_kbps / 1000.0 avg_d_mbps, avg_u_kbps / 1000.0 avg_u_mbps,
               avg_lat_ms, tests, devices, tile_lat, tile_lon
        from '{PROCESSED / "ookla_tiles.parquet"}'""")

    con.execute("create table fx_rates(currency varchar primary key, usd_per_unit double, source varchar, as_of varchar)")
    con.executemany("insert into fx_rates values (?, ?, ?, ?)", fetch_fx({c["currency"] for c in CITY_LIST}))

    con.execute(f"""
        create view neighbourhood_summary as
        select l.city, l.neighbourhood, any_value(l.neighbourhood_group) neighbourhood_group,
               count(*) n_listings,
               count(*) filter (where l.has_workspace) n_with_workspace,
               median(l.price_local) median_nightly_local,
               round(median(l.price_local * f.usd_per_unit), 2) median_nightly_usd,
               round(median(t.avg_d_mbps), 1) median_d_mbps_fixed,
               round(avg(case when coalesce(t.tests, 0) < {LOW_CONFIDENCE_TESTS} then 1 else 0 end), 2) share_low_confidence
        from listings l
        join cities c on c.city = l.city
        join fx_rates f on f.currency = c.currency
        left join speed_tiles t on t.city = l.city and t.quadkey = l.quadkey and t.type = 'fixed'
        group by l.city, l.neighbourhood""")

    # Climate averages fetched earlier by build_climate.py (run it after a first build)
    climate = PROCESSED / "climate.parquet"
    if climate.exists():
        con.execute(f"create table climate as select * from '{climate}' order by city, month")
    else:
        print("no climate.parquet yet: run scripts/build_climate.py after this build")

    con.execute("create index availability_listing on availability(listing_id)")
    con.execute(f"create table build_info as select '{date.today()}' built_on, {MIN_STAY} min_stay_nights, {LOW_CONFIDENCE_TESTS} low_confidence_tests")

    # Self-checks: the SQL quadkey matches mercantile's, the join works, and nothing is empty
    sample = con.execute("select lat, lon, quadkey from listings using sample 2000 rows").fetchall()
    assert all(quadkey16(lat, lon) == qk for lat, lon, qk in sample), "SQL quadkey disagrees with mercantile"
    for table in ("cities", "listings", "availability", "speed_tiles", "fx_rates"):
        n = con.execute(f"select count(*) from {table}").fetchone()[0]
        assert n > 0, f"{table} is empty"
        print(f"{table:14} {n:>10,} rows")
    joined = con.execute("""select avg(case when t.quadkey is not null then 1 else 0 end) from listings l
        left join speed_tiles t on t.city = l.city and t.quadkey = l.quadkey and t.type = 'fixed'""").fetchone()[0]
    print(f"listings with a fixed speed tile: {joined:.1%}")
    assert joined > 0.9, "quadkey join looks broken"
    # Every rename must have matched a real neighbourhood, or the source's spelling changed
    for city, names in RENAME.items():
        found = {r[0] for r in con.execute(
            "select distinct neighbourhood_local from listings where city = ?", [city]).fetchall()}
        assert not (unused := set(names) - found), f"{city} renames matched nothing: {unused}"

    con.close()
    tmp.rename(DB)
    print(f"\nwrote {DB} ({DB.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
