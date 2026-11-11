"""find_stays and neighbourhood_speed: the spatial join of listings to Ookla tiles, packaged
so a Researcher doesn't have to write the date and tile SQL itself."""

from datetime import date

import mercantile

from tools.db import cite_airbnb, cite_fx, cite_ookla, city_info, cursor, error, fold, fx_row, rows, to_json

LOW_CONFIDENCE_TESTS = 10  # fewer Ookla tests than this in a tile: low confidence
MIN_NIGHTLY_USD = 1  # below this a price is broken data. ($1-10 is real: dorm beds and rooms in Istanbul, Bangkok, ...)
DEFAULT_MAX_BEDROOMS = 2  # one person (or a couple) with room for an office; studios (0 or unknown) are kept


def _neighbourhood(city: str, name: str | None) -> str | None:
    """The stored name for a loosely typed neighbourhood or neighbourhood group (municipality /
    district, where the city has them). Matches the display name or the original-script name,
    ignoring case and accents. LookupError lists the options."""
    if not name:
        return None
    found = rows("""select neighbourhood, neighbourhood_local, neighbourhood_group, count(*) n
                    from listings where city = ? group by all order by n desc""", [city])
    key = fold(name)
    # Exact match first: a neighbourhood (display or original name), then a group
    for r in found:
        if key in (fold(r["neighbourhood"]), fold(r["neighbourhood_local"] or "")):
            return r["neighbourhood"]
    groups = list(dict.fromkeys(r["neighbourhood_group"] for r in found if r["neighbourhood_group"]))
    for g in groups:
        if key == fold(g):
            return g
    # Then a unique partial match ('Plaka' -> 'Commercial Triangle-Plaka')
    names = list(dict.fromkeys(r["neighbourhood"] for r in found))
    close = [n for n in names + groups if key in fold(n)]
    if len(close) == 1:
        return close[0]
    hint = f" Groups (municipalities/districts): {', '.join(groups)}." if groups else ""
    if len(close) > 1:
        raise LookupError(f"'{name}' matches several in {city}: {', '.join(close[:10])}. Pick one.")
    raise LookupError(f"No neighbourhood '{name}' in {city}. Largest: {', '.join(names[:25])}.{hint}")


def find_stays(
    city: str,
    check_in: str,
    check_out: str,
    max_total_usd: float | None = None,
    needs_workspace: bool = False,
    room_type: str | None = None,
    min_bedrooms: int | None = None,
    max_bedrooms: int | None = DEFAULT_MAX_BEDROOMS,
    guests: int = 1,
    neighbourhood: str | None = None,
) -> str:
    """Listings open for every night from check_in to check_out whose host accepts that length,
    priced for the whole stay in USD, grouped by neighbourhood with measured internet speed.
    By default only listings with at most 2 bedrooms; max_bedrooms=None lifts the cap."""
    try:
        c = city_info(city)
        nb = _neighbourhood(c["city"], neighbourhood)
        d_in, d_out = date.fromisoformat(check_in), date.fromisoformat(check_out)
        fx = fx_row(c["currency"])
    except (LookupError, ValueError) as e:
        return error(str(e))
    nights = (d_out - d_in).days
    if nights < 7:
        return error(f"Stays must be at least 7 nights; {check_in} to {check_out} is {nights}.")
    snapshot = c["airbnb_snapshot_date"]
    last_day = rows("select max(end_date) d from availability where city = ?", [c["city"]])[0]["d"]
    if d_in < snapshot or d_out > last_day:
        return error(f"{c['city']}'s calendar covers {snapshot} to {last_day} (the snapshot's next 12 months). "
                     f"{check_in} to {check_out} is outside it.")

    # A stay fits an open stretch if the stretch covers every night (check_out is the morning you leave).
    base = f"""
        with fit as (
            select l.*, l.price_local * {fx['usd_per_unit']} nightly_usd,
                   l.price_local * {fx['usd_per_unit']} * {nights} total_usd,
                   t.avg_d_mbps, t.avg_u_mbps, t.avg_lat_ms, coalesce(t.tests, 0) tests
            from listings l
            left join speed_tiles t on t.city = l.city and t.quadkey = l.quadkey and t.type = 'fixed'
            where l.city = $city
              and exists (select 1 from availability a where a.listing_id = l.listing_id
                          and a.start_date <= $d_in and a.end_date >= $d_out - 1 and a.min_nights_max <= $nights)
              and coalesce(l.minimum_nights, 1) <= $nights and coalesce(l.maximum_nights, 100000) >= $nights
              and l.price_local * {fx['usd_per_unit']} >= {MIN_NIGHTLY_USD}
              and coalesce(l.accommodates, 1) >= $guests
              and (not $ws::boolean or l.has_workspace)
              and ($rt::varchar is null or l.room_type = $rt::varchar)
              and ($br::int is null or l.bedrooms >= $br::int)
              and ($max_br::int is null or coalesce(l.bedrooms, 0) <= $max_br::int)
              and ($nb::varchar is null or l.neighbourhood = $nb::varchar or l.neighbourhood_group = $nb::varchar)
        ),
        ok as (select * from fit where $max::double is null or total_usd <= $max::double)"""
    params = {"city": c["city"], "d_in": d_in, "d_out": d_out, "nights": nights, "guests": guests,
              "ws": needs_workspace, "rt": room_type, "br": min_bedrooms, "max_br": max_bedrooms, "nb": nb,
              "max": max_total_usd}

    def q(select: str) -> list[dict]:
        cur = cursor()
        cur.execute(base + select, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    totals = q("""select (select count(*) from fit) n_available, (select count(*) from ok) n_within_budget,
        (select round(quantile_cont(total_usd, 0.10)) from fit) p10_total_usd,
        (select round(median(total_usd)) from fit) median_total_usd,
        (select round(min(total_usd)) from fit) cheapest_total_usd""")[0]
    by_nb = q(f"""select neighbourhood, any_value(neighbourhood_group) neighbourhood_group,
            count(*) n_listings, round(median(total_usd)) median_total_usd,
            round(min(total_usd)) min_total_usd, round(median(avg_d_mbps)) median_down_mbps_fixed,
            round(median(avg_u_mbps)) median_up_mbps_fixed,
            round(avg(case when tests < {LOW_CONFIDENCE_TESTS} then 1 else 0 end), 2) share_low_confidence,
            count(*) filter (where wifi_mbps_listed is not null) n_with_airbnb_wifi_test,
            round(median(rating), 2) median_rating
        from ok group by neighbourhood order by n_listings desc limit 15""")
    examples = q(f"""select listing_id, name, listing_url, neighbourhood, neighbourhood_group, room_type, bedrooms,
            round(nightly_usd) nightly_usd, round(total_usd) total_usd, has_workspace,
            wifi_mbps_listed, round(avg_d_mbps) tile_down_mbps_fixed, tests tile_tests, rating, n_reviews
        from ok order by (n_reviews >= 5 and rating >= 4.7) desc, total_usd limit 5""")

    return to_json({
        "city": c["city"],
        "stay": {"check_in": check_in, "check_out": check_out, "nights": nights},
        "filters": {"max_total_usd": max_total_usd, "needs_workspace": needs_workspace, "room_type": room_type,
                    "min_bedrooms": min_bedrooms, "max_bedrooms": max_bedrooms, "guests": guests,
                    "neighbourhood": nb},
        **totals,
        "by_neighbourhood": by_nb,
        "examples": examples,
        "notes": [
            f"Price = the listing's nightly rate in the {snapshot} snapshot x {nights} nights, converted at "
            f"{fx['as_of']} rates. Not a live quote; weekly/monthly discounts are not reflected, so long stays may cost less.",
            f"Availability is as of {snapshot}; some of these dates may have been booked since.",
            f"Speeds are Ookla fixed-broadband medians for the ~600 m tile around each listing, not the unit's own "
            f"Wi-Fi. A tile with fewer than {LOW_CONFIDENCE_TESTS} tests is low confidence. wifi_mbps_listed, when "
            f"present, is Airbnb's own speed test of the unit.",
            "Includes all room types (entire homes, private rooms, shared rooms/dorm beds, hotel rooms) unless "
            "room_type is set; check room_type before calling a cheap listing an apartment."
            + (f" Only listings with at most {max_bedrooms} bedrooms (studios included)." if max_bedrooms is not None else ""),
            f"Listings under ${MIN_NIGHTLY_USD}/night are excluded as broken prices.",
            "neighbourhood_group, when set, is the municipality or district. Some cities' data covers the wider "
            "metro area (e.g. Lisbon includes Sintra and Cascais); filter with neighbourhood=<group> for the city proper.",
        ],
        "citations": [cite_airbnb(c, "listings + availability"), cite_ookla(c), cite_fx(fx)],
    })


def _neighbours(quadkey: str) -> list[str]:
    t = mercantile.quadkey_to_tile(quadkey)
    return [mercantile.quadkey(mercantile.Tile(t.x + dx, t.y + dy, t.z)) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]


def neighbourhood_speed(city: str, neighbourhood: str | None = None) -> str:
    """Measured internet speed around a city's listings, per neighbourhood (or for one), from the
    Ookla tile each listing sits in. For one neighbourhood, also a widened 3x3-tile estimate."""
    try:
        c = city_info(city)
        nb = _neighbourhood(c["city"], neighbourhood)
    except LookupError as e:
        return error(str(e))

    per_nb = rows(f"""
        select l.neighbourhood, any_value(l.neighbourhood_group) neighbourhood_group, count(*) n_listings,
               round(median(f.avg_d_mbps)) median_down_mbps_fixed, round(median(f.avg_u_mbps)) median_up_mbps_fixed,
               round(median(f.avg_lat_ms)) median_latency_ms_fixed,
               round(median(m.avg_d_mbps)) median_down_mbps_mobile,
               count(distinct l.quadkey) n_tiles,
               round(avg(case when coalesce(f.tests, 0) < {LOW_CONFIDENCE_TESTS} then 1 else 0 end), 2) share_low_confidence,
               count(l.wifi_mbps_listed) n_with_airbnb_wifi_test,
               round(median(l.wifi_mbps_listed)) median_airbnb_wifi_test_mbps
        from listings l
        left join speed_tiles f on f.city = l.city and f.quadkey = l.quadkey and f.type = 'fixed'
        left join speed_tiles m on m.city = l.city and m.quadkey = l.quadkey and m.type = 'mobile'
        where l.city = $1 and ($2::varchar is null or l.neighbourhood = $2::varchar or l.neighbourhood_group = $2::varchar)
        group by l.neighbourhood order by n_listings desc limit 25""", [c["city"], nb])

    result = {"city": c["city"], "neighbourhoods": per_nb}

    if nb:
        # Widen to the 3x3 block (~1.8 km) around each listing's tile: tests-weighted mean download.
        tiles = {r["quadkey"]: r for r in rows(
            "select quadkey, avg_d_mbps, tests from speed_tiles where city = ? and type = 'fixed'", [c["city"]])}
        listing_tiles = rows("""select quadkey, count(*) n from listings
                                where city = $1 and (neighbourhood = $2 or neighbourhood_group = $2) group by 1""",
                             [c["city"], nb])
        block = {q for lt in listing_tiles for q in _neighbours(lt["quadkey"])}
        measured = [tiles[q] for q in block if q in tiles]
        tests = sum(t["tests"] for t in measured)
        result["widened_3x3"] = {
            "tiles": len(measured),
            "tests": tests,
            "tests_weighted_mean_down_mbps_fixed": round(sum(t["avg_d_mbps"] * t["tests"] for t in measured) / tests)
            if tests else None,
        }

    result["notes"] = [
        "Medians are across listings: each listing gets the speed measured in its ~600 m Ookla tile.",
        f"share_low_confidence = listings whose tile has fewer than {LOW_CONFIDENCE_TESTS} fixed tests.",
        "median_airbnb_wifi_test_mbps comes from the subset of listings showing Airbnb's own Wi-Fi speed test.",
    ]
    result["citations"] = [cite_airbnb(c, "listings"), cite_ookla(c, "fixed"), cite_ookla(c, "mobile")]
    return to_json(result)
