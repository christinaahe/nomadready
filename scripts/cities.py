"""The cities NomadReady covers. Every offline script and the app read this one list.

slug: the city's folder name in Inside Airbnb's URLs (the third path segment),
      e.g. data.insideairbnb.com/portugal/lisbon/lisbon/2026-09-19/data/listings.csv.gz
currency: what Inside Airbnb's `price` field is in for that city (local currency).
"""

CITIES = [
    # city,           slug,              country,          iso2, timezone,                         currency
    ("Lisbon",        "lisbon",          "Portugal",       "PT", "Europe/Lisbon",                  "EUR"),
    ("Porto",         "porto",           "Portugal",       "PT", "Europe/Lisbon",                  "EUR"),
    ("Barcelona",     "barcelona",       "Spain",          "ES", "Europe/Madrid",                  "EUR"),
    ("Madrid",        "madrid",          "Spain",          "ES", "Europe/Madrid",                  "EUR"),
    ("Rome",          "rome",            "Italy",          "IT", "Europe/Rome",                    "EUR"),
    ("Athens",        "athens",          "Greece",         "GR", "Europe/Athens",                  "EUR"),
    ("Berlin",        "berlin",          "Germany",        "DE", "Europe/Berlin",                  "EUR"),
    ("Amsterdam",     "amsterdam",       "Netherlands",    "NL", "Europe/Amsterdam",               "EUR"),
    ("Prague",        "prague",          "Czech Republic", "CZ", "Europe/Prague",                  "CZK"),
    ("Istanbul",      "istanbul",        "Turkey",         "TR", "Europe/Istanbul",                "TRY"),
    ("Cape Town",     "cape-town",       "South Africa",   "ZA", "Africa/Johannesburg",            "ZAR"),
    ("Mexico City",   "mexico-city",     "Mexico",         "MX", "America/Mexico_City",            "MXN"),
    ("Buenos Aires",  "buenos-aires",    "Argentina",      "AR", "America/Argentina/Buenos_Aires", "ARS"),
    ("Rio de Janeiro","rio-de-janeiro",  "Brazil",         "BR", "America/Sao_Paulo",              "BRL"),
    ("Santiago",      "santiago",        "Chile",          "CL", "America/Santiago",               "CLP"),
    ("Montreal",      "montreal",        "Canada",         "CA", "America/Toronto",                "CAD"),
    ("Vancouver",     "vancouver",       "Canada",         "CA", "America/Vancouver",              "CAD"),
    ("Bangkok",       "bangkok",         "Thailand",       "TH", "Asia/Bangkok",                   "THB"),
    ("Tokyo",         "tokyo",           "Japan",          "JP", "Asia/Tokyo",                     "JPY"),
    ("Taipei",        "taipei",          "Taiwan",         "TW", "Asia/Taipei",                    "TWD"),
]

FIELDS = ("city", "slug", "country", "iso2", "timezone", "currency")
CITY_LIST = [dict(zip(FIELDS, row)) for row in CITIES]
