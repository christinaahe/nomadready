"""U.S. State Department travel advisory level for a city's country. Live, no key."""

import html
import re
import time

import httpx

from tools.db import city_info, error, to_json

ADVISORIES_URL = "https://cadataapi.state.gov/api/TravelAdvisories"
CACHE_S = 3600

# The API keys countries by FIPS 10-4 code, not ISO 3166: Chile is CI, Czechia EZ, Japan JA, ...
ISO2_TO_FIPS = {
    "PT": "PO", "ES": "SP", "IT": "IT", "GR": "GR", "DE": "GM", "NL": "NL", "CZ": "EZ", "TR": "TU",
    "ZA": "SF", "MX": "MX", "AR": "AR", "BR": "BR", "CL": "CI", "CA": "CA", "TH": "TH", "JP": "JA", "TW": "TW",
}

_cache: dict = {"at": 0.0, "by_code": {}}


def _advisories() -> dict[str, dict]:
    if time.time() - _cache["at"] > CACHE_S:
        data = httpx.get(ADVISORIES_URL, timeout=30, follow_redirects=True).json()
        _cache["by_code"] = {code: a for a in data for code in a.get("Category", [])}
        _cache["at"] = time.time()
    return _cache["by_code"]


def travel_advisory(city: str) -> str:
    """The advisory level (1-4) and link for the country a covered city is in."""
    try:
        c = city_info(city)
    except LookupError as e:
        return error(str(e))
    fips = ISO2_TO_FIPS.get(c["iso2"])
    try:
        advisory = _advisories().get(fips)
    except (httpx.HTTPError, ValueError) as e:
        return error(f"State Department API failed: {type(e).__name__}: {e}")
    if not advisory:
        return error(f"No advisory found for {c['country']} (code {fips}). Record it as unknown.")
    level = re.search(r"Level (\d)", advisory["Title"])
    summary = html.unescape(re.sub(r"<[^>]+>", " ", advisory.get("Summary", "")))
    summary = re.sub(r"\s+", " ", summary).strip()
    return to_json({
        "country": c["country"],
        "level": int(level.group(1)) if level else None,
        "title": advisory["Title"],
        "updated": advisory.get("Updated", "")[:10],
        "link": advisory["Link"],
        "summary_start": summary[:300],
        "notes": ["Country-wide level from the U.S. State Department. Report the level and link; don't characterize "
                  "safety beyond it. It says nothing about a specific neighbourhood."],
        "citations": [{
            "id": f"state-dept-{c['iso2'].lower()}",
            "source": "U.S. Department of State travel advisories",
            "detail": f"{advisory['Title']} (updated {advisory.get('Updated', '')[:10]})",
            "url": advisory["Link"],
        }],
    })
