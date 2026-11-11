"""When your home-time-zone work hours fall in a city's local time. Computed with zoneinfo."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tools.db import city_info, error, to_json

DAY_START, DAY_END = time(8, 0), time(22, 0)  # "reasonable" local hours to be online
STEP = timedelta(minutes=15)

# Common names people type, mapped to IANA zones
ALIASES = {
    "eastern": "America/New_York", "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
    "new york": "America/New_York", "nyc": "America/New_York",
    "central": "America/Chicago", "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
    "mountain": "America/Denver", "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
    "pacific": "America/Los_Angeles", "pt": "America/Los_Angeles", "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles", "san francisco": "America/Los_Angeles", "los angeles": "America/Los_Angeles",
    "london": "Europe/London", "uk": "Europe/London", "gmt": "Etc/GMT", "utc": "UTC",
    "cet": "Europe/Berlin", "india": "Asia/Kolkata", "ist": "Asia/Kolkata", "sydney": "Australia/Sydney",
}


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(ALIASES.get(name.strip().lower(), name.strip()))
    except (ZoneInfoNotFoundError, ValueError):
        raise LookupError(f"Unknown time zone '{name}'. Use an IANA name like 'America/New_York'.") from None


def _clock(text: str) -> time:
    text = text.strip().lower().replace(" ", "")
    for fmt in ("%H:%M", "%H", "%I%p", "%I:%M%p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"Can't read '{text}' as a time. Use 'HH:MM', e.g. '09:00'.")


def work_overlap(city: str, home_tz: str, work_start: str, work_end: str, on_date: str | None = None) -> str:
    """Where home-time-zone work hours land in the city's local time, on a given date (for DST),
    and how many of those hours fall outside 08:00-22:00 local."""
    try:
        c = city_info(city)
        home, local = _zone(home_tz), ZoneInfo(c["timezone"])
        start_t, end_t = _clock(work_start), _clock(work_end)
        day = date.fromisoformat(on_date) if on_date else date.today()
    except (LookupError, ValueError) as e:
        return error(str(e))

    start = datetime.combine(day, start_t, home)
    end = datetime.combine(day, end_t, home)
    if end <= start:  # an overnight shift
        end += timedelta(days=1)

    total = night = timedelta()
    t = start
    while t < end:
        lt = t.astimezone(local).time()
        total += STEP
        if not (DAY_START <= lt < DAY_END):
            night += STEP
        t += STEP

    ls, le = start.astimezone(local), end.astimezone(local)
    offset_h = (ls.utcoffset() - start.utcoffset()).total_seconds() / 3600
    night_h = night.total_seconds() / 3600
    verdict = "all within 08:00-22:00 local" if night_h == 0 else (
        "mostly late night or early morning" if night_h > total.total_seconds() / 7200 else "partly outside 08:00-22:00 local")
    return to_json({
        "city": c["city"],
        "home_tz": str(home), "city_tz": c["timezone"], "date": day.isoformat(),
        "city_ahead_of_home_hours": offset_h,
        "work_hours_home": f"{start:%H:%M}-{end:%H:%M}",
        "work_hours_local": f"{ls:%H:%M}-{le:%H:%M}" + (" (+1 day)" if le.date() > ls.date() else ""),
        "hours_total": total.total_seconds() / 3600,
        "hours_outside_8am_10pm_local": night_h,
        "verdict": verdict,
        # The id needs a dash: that's how citation ids are told apart from other [bracketed] text
        "citations": [{"id": "iana-tz","source": "Computed with the IANA time zone database (Python zoneinfo)",
                       "detail": f"{home} -> {c['timezone']} on {day.isoformat()}"}],
    })
