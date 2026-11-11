"""Exchange rate to USD for a city's currency, from the fx_rates table (fetched at build time)."""

from tools.db import cite_fx, city_info, error, fx_row, to_json


def fx_to_usd(city_or_currency: str) -> str:
    """USD per unit of a currency. Accepts a currency code (EUR) or a covered city (Lisbon)."""
    code = city_or_currency.strip().upper()
    try:
        if len(code) != 3 or not code.isalpha():
            code = city_info(city_or_currency)["currency"]
        fx = fx_row(code)
    except LookupError as e:
        return error(str(e))
    return to_json({
        "currency": fx["currency"],
        "usd_per_unit": fx["usd_per_unit"],
        "units_per_usd": round(1 / fx["usd_per_unit"], 4),
        "as_of": fx["as_of"],
        "notes": ["Official/reference rate. For ARS the rate visitors actually get can differ substantially."]
        if fx["currency"] == "ARS" else [],
        "citations": [cite_fx(fx)],
    })
