"""Typical weather for a month in a city: averages of Open-Meteo's historical archive (ERA5).

The averages are computed once, by scripts/build_climate.py, and stored in the `climate` table, so
the tool makes no live call. A live call cost 65 of Open-Meteo's 600 free calls per minute (5 years
of daily data are weighted as ~130 two-week chunks), and requests from Cloud Run share IP
addresses with other apps, so live calls failed with 429s in testing. Open-Meteo data is CC BY 4.0.
"""

import calendar

import httpx

from tools.db import city_info, error, rows, to_json

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
YEARS = 5  # the last 5 complete years before the build
RAIN_DAY_MM = 1.0
DAILY_VARS = "temperature_2m_max,temperature_2m_min,precipitation_sum,sunshine_duration,daylight_duration"


def _month_number(month: str | int) -> int:
    if isinstance(month, int) or str(month).isdigit():
        n = int(month)
    else:
        names = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
        names |= {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
        n = names.get(str(month).strip().lower(), 0)
    if not 1 <= n <= 12:
        raise ValueError(f"'{month}' isn't a month. Use 1-12 or a month name.")
    return n


# --- Build time (scripts/build_climate.py) ---


def fetch_daily(lat: float, lon: float, first_year: int, last_year: int) -> httpx.Response:
    return httpx.get(ARCHIVE_URL, params={
        "latitude": lat, "longitude": lon,
        "start_date": f"{first_year}-01-01", "end_date": f"{last_year}-12-31",
        "daily": DAILY_VARS, "timezone": "auto",
    }, timeout=60)


def monthly_normals(daily: dict, n_years: int) -> list[dict]:
    """12 rows of monthly averages from Open-Meteo's daily arrays."""
    out = []
    for m in range(1, 13):
        days = [i for i, d in enumerate(daily["time"]) if int(d[5:7]) == m]

        def avg(key, scale=1.0):
            vals = [daily[key][i] for i in days if daily[key][i] is not None]
            return round(sum(vals) / len(vals) * scale, 1) if vals else None

        precip = [daily["precipitation_sum"][i] or 0 for i in days]
        out.append({
            "month": m,
            "avg_high_c": avg("temperature_2m_max"),
            "avg_low_c": avg("temperature_2m_min"),
            "rainy_days_per_month": round(sum(1 for p in precip if p >= RAIN_DAY_MM) / n_years, 1),
            "avg_precip_mm_per_month": round(sum(precip) / n_years, 1),
            "avg_sunshine_hours_per_day": avg("sunshine_duration", 1 / 3600),
            "avg_daylight_hours_per_day": avg("daylight_duration", 1 / 3600),
        })
    return out


# --- The tool ---


def month_climate(city: str, month: str | int) -> str:
    """Average daily high/low, rainy days, sunshine and daylight for a month, over 5 recent years."""
    try:
        c = city_info(city)
        m = _month_number(month)
    except (LookupError, ValueError) as e:
        return error(str(e))
    try:
        found = rows("select * from climate where city = $1 and month = $2", [c["city"], m])
    except Exception:  # an old database without the table
        found = []
    if not found:
        return error(f"No climate data for {c['city']}. Report the weather as unknown.")
    r = found[0]
    hi, lo = r["avg_high_c"], r["avg_low_c"]
    span = f"{calendar.month_name[m]} {r['years']}"
    return to_json({
        "city": c["city"],
        "month": calendar.month_name[m],
        "years": r["years"],
        "avg_high_c": hi, "avg_low_c": lo,
        "avg_high_f": round(hi * 9 / 5 + 32, 1) if hi is not None else None,
        "avg_low_f": round(lo * 9 / 5 + 32, 1) if lo is not None else None,
        "rainy_days_per_month": r["rainy_days_per_month"],
        "avg_precip_mm_per_month": r["avg_precip_mm_per_month"],
        "avg_sunshine_hours_per_day": r["avg_sunshine_hours_per_day"],
        "avg_daylight_hours_per_day": r["avg_daylight_hours_per_day"],
        "notes": [f"Averages of daily values for {span} at the city's center (median listing location). "
                  f"A rainy day has at least {RAIN_DAY_MM} mm of precipitation. Past weather, not a forecast."],
        "citations": [{
            "id": f"open-meteo-{c['slug']}-{m:02d}",
            "source": "Open-Meteo Historical Weather API (ERA5 reanalysis), CC BY 4.0",
            "detail": f"{c['city']}, {span}, lat {r['lat']:.3f}, lon {r['lon']:.3f}, retrieved {r['retrieved_on']}",
            "url": "https://open-meteo.com",
        }],
    })
