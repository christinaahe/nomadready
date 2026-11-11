"""The DuckDB connection, city lookup, citations, and the read-only SQL tool."""

import json
import os
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("NOMAD_DB", ROOT / "data" / "nomad.duckdb"))

MAX_ROWS = 50  # query_listings returns at most this many rows
QUERY_TIMEOUT_S = 15
TABLES = ("cities", "listings", "availability", "speed_tiles", "fx_rates", "neighbourhood_summary")

_con: duckdb.DuckDBPyConnection | None = None
_con_lock = threading.Lock()


def cursor() -> duckdb.DuckDBPyConnection:
    """A cursor on the shared read-only connection. Cursors are safe to use from parallel threads.
    External access is off, so SQL can't read local files or URLs (read_csv('/etc/passwd'), etc.)."""
    global _con
    with _con_lock:
        if _con is None:
            if not DB_PATH.exists():
                raise FileNotFoundError(f"{DB_PATH} not found. Run scripts/build_db.py or download it from GCS.")
            _con = duckdb.connect(
                str(DB_PATH), read_only=True,
                config={"enable_external_access": False, "lock_configuration": True},
            )
        return _con.cursor()


def rows(sql: str, params: list | None = None) -> list[dict]:
    cur = cursor()
    cur.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def to_json(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def error(message: str, **extra) -> str:
    return to_json({"error": message, **extra})


def fold(text: str) -> str:
    """Lowercase, accents removed: 'Cuauhtémoc' and 'cuauhtemoc' match."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).lower().strip()


@lru_cache(maxsize=1)
def _cities() -> dict[str, dict]:
    return {fold(c["city"]): c for c in rows("select * from cities")}


def city_info(name: str) -> dict:
    """The cities row for a name or slug, case- and accent-insensitive. LookupError lists the options."""
    cities = _cities()
    key = fold(name).replace("-", " ")
    for k, c in cities.items():
        if key in (k, c["slug"].replace("-", " ")):
            return c
    raise LookupError(f"'{name}' isn't covered. Covered cities: {', '.join(c['city'] for c in cities.values())}")


# --- Citations: every number a tool returns carries one. The Writer turns ids into a Sources list. ---


def cite_airbnb(city: dict, tables: str) -> dict:
    return {
        "id": f"airbnb-{city['slug']}",
        "source": "Inside Airbnb",
        "detail": f"{city['city']}, snapshot {city['airbnb_snapshot_date']}, {tables}",
        "url": "https://insideairbnb.com/get-the-data/",
    }


def cite_ookla(city: dict, kind: str = "fixed") -> dict:
    return {
        "id": f"ookla-{kind}-{city['ookla_quarter']}",
        "source": "Speedtest® by Ookla® Global Fixed and Mobile Network Performance Maps",
        "detail": f"{kind} tiles, {city['ookla_quarter']}, zoom-16 (~600 m) tiles joined to listings by quadkey",
        "url": "https://github.com/teamookla/ookla-open-data",
    }


def cite_fx(fx: dict) -> dict:
    return {
        "id": f"fx-{fx['currency']}",
        "source": fx["source"],
        "detail": f"1 {fx['currency']} = {fx['usd_per_unit']:.6g} USD, as of {fx['as_of']}",
    }


def fx_row(currency: str) -> dict:
    found = rows("select * from fx_rates where currency = ?", [currency.upper()])
    if not found:
        raise LookupError(f"No exchange rate for {currency}")
    return found[0]


# --- The tool ---


def query_listings(sql: str) -> str:
    """Run one read-only SELECT against the NomadReady database. Returns at most MAX_ROWS rows."""
    sql = sql.strip().rstrip(";").strip()
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        return error(f"SQL doesn't parse: {e}")
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        return error("Only a single SELECT (or WITH ... SELECT) statement is allowed.")

    cur = cursor()
    # DuckDB has no statement timeout: interrupt the cursor from a timer instead.
    timer = threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)
    timer.start()
    try:
        cur.execute(f"select * from ({sql}) limit {MAX_ROWS + 1}")
        cols = [d[0] for d in cur.description]
        result = [dict(zip(cols, r)) for r in cur.fetchall()]
    except duckdb.InterruptException:
        return error(f"Query took longer than {QUERY_TIMEOUT_S}s. Filter by city first, or aggregate.")
    except duckdb.Error as e:
        return error(f"{type(e).__name__}: {str(e)[:400]}", tables=list(TABLES))
    finally:
        timer.cancel()

    # Cite every covered city the query names, with its snapshot date.
    folded = fold(sql)
    cited = [c for k, c in _cities().items() if k in folded or c["slug"] in folded]
    used = [t for t in TABLES if t in sql.lower()]
    citations = [cite_airbnb(c, ", ".join(used) or "query") for c in cited]
    if "speed_tiles" in used or "neighbourhood_summary" in used:
        citations += [cite_ookla(cited[0] if cited else next(iter(_cities().values())))]
    return to_json({
        "columns": cols,
        "rows": result[:MAX_ROWS],
        "truncated": len(result) > MAX_ROWS,
        "query": sql,
        "citations": citations,
    })
