"""Coworking spaces near a neighbourhood's listings, from the Google Places API (New), live.

Used when a stay has no dedicated workspace (a shared room, or listings without one). Google's
terms don't allow storing Places content, so nothing is cached or written to the database.
Needs GOOGLE_PLACES_API_KEY (a key restricted to places.googleapis.com); without one, the tool
returns an error the agent reports as "coworking unknown".
"""

import math
import os
import re
from datetime import date

import httpx

from tools.db import city_info, error, fold, rows, to_json
from tools.stays import _neighbourhood

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
RADIUS_KM = 1.5  # half-width of the search box around the neighbourhood's center
FIELDS = ",".join(f"places.{f}" for f in (
    "displayName", "formattedAddress", "location", "rating", "userRatingCount", "primaryType", "types",
    "websiteUri", "googleMapsUri", "regularOpeningHours.weekdayDescriptions", "businessStatus",
))
MAX_RETURNED = 10  # the model needs a shortlist, not every result


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def nearby_coworking(city: str, neighbourhood: str | None = None) -> str:
    """Coworking spaces within ~1.5 km of the center of a neighbourhood's listings (or the city's)."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        return error("Coworking search isn't configured (no GOOGLE_PLACES_API_KEY). Report coworking as unknown.")
    try:
        c = city_info(city)
        nb = _neighbourhood(c["city"], neighbourhood)
    except LookupError as e:
        return error(str(e))

    lat, lon = rows("""select median(lat) lat, median(lon) lon from listings
                       where city = $1 and ($2::varchar is null or neighbourhood = $2 or neighbourhood_group = $2)""",
                    [c["city"], nb])[0].values()
    dlat = RADIUS_KM / 111.0
    dlon = RADIUS_KM / (111.0 * math.cos(math.radians(lat)))
    try:
        r = httpx.post(SEARCH_URL, timeout=30, headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS}, json={
            "textQuery": "coworking space",
            "pageSize": 20,
            "languageCode": "en",
            "locationRestriction": {"rectangle": {"low": {"latitude": lat - dlat, "longitude": lon - dlon},
                                                  "high": {"latitude": lat + dlat, "longitude": lon + dlon}}},
        })
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return error(f"Google Places request failed: {type(e).__name__}: {e}. Report coworking as unknown.")
    if r.status_code != 200:
        status = body.get("error", {}).get("status", r.status_code)
        hint = " The daily quota may be used up." if r.status_code == 429 else ""
        return error(f"Google Places returned {status}.{hint} Report coworking as unknown.")

    places = []
    for p in body.get("places", []):
        if p.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
            continue
        loc = p.get("location", {})
        places.append({
            "name": p.get("displayName", {}).get("text"),
            "is_coworking": "coworking_space" in p.get("types", []),  # Google's category, not just a name match
            "rating": p.get("rating"),
            "n_ratings": p.get("userRatingCount", 0),
            "distance_km": round(_km(lat, lon, loc["latitude"], loc["longitude"]), 2) if loc else None,
            "address": p.get("formattedAddress"),
            "website": p.get("websiteUri"),
            "google_maps_url": p.get("googleMapsUri"),
            "hours": (p.get("regularOpeningHours") or {}).get("weekdayDescriptions"),
        })
    cowork = [p for p in places if p["is_coworking"]]
    # Coworking first, then the ones with the most ratings (a rating from 3 people says little)
    places.sort(key=lambda p: (not p["is_coworking"], -(p["n_ratings"] or 0)))
    well_rated = [p for p in cowork if (p["rating"] or 0) >= 4.5 and p["n_ratings"] >= 20]

    area = nb or f"{c['city']} (city-wide center)"
    return to_json({
        "city": c["city"],
        "neighbourhood": nb,
        "search_center": {"lat": round(lat, 5), "lon": round(lon, 5)},
        "n_coworking": len(cowork),
        "n_well_rated": len(well_rated),
        "capped_at_20": len(body.get("places", [])) >= 20,
        "places": places[:MAX_RETURNED],
        "notes": [
            f"Search: 'coworking space' within ~{RADIUS_KM} km of the median location of {area}'s listings. "
            "Google returns at most 20 results; capped_at_20 = true means there are probably more.",
            "is_coworking is Google's own category; other results matched the search but are categorized as "
            "something else (offices, cafés, associations).",
            "Well rated = 4.5+ stars from 20+ ratings. Ratings say nothing about day-pass prices, desk "
            "availability, or community; check the website.",
        ],
        "citations": [{
            # ASCII ids: 'Misericórdia' -> 'misericordia', so the Writer's [id] matches the citation pattern
            "id": f"google-places-{c['slug']}" + (f"-{re.sub(r'[^a-z0-9]+', '-', fold(nb)).strip('-')}" if nb else ""),
            "source": "Google Maps (Places API)",
            "detail": f"text search 'coworking space' near {area}, retrieved {date.today().isoformat()}",
            "url": "https://maps.google.com",
        }],
    })
