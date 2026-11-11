"""Download Ookla's global fixed and mobile speed tiles for one quarter, once, and keep only the
tiles around each city's Airbnb listings.

Each city's box is the extent of its listings (from the Inside Airbnb files download_airbnb.py
fetched), plus BUFFER_DEG on every side so a listing at the edge still has neighbor tiles.

Writes data/processed/ookla_tiles.parquet with columns
    city, type, quadkey, avg_d_kbps, avg_u_kbps, avg_lat_ms, tests, devices, tile_lat, tile_lon
and data/processed/ookla_meta.json with the quarter used.

    uv run scripts/download_ookla.py                 # newest quarter on S3
    uv run scripts/download_ookla.py --quarter 2026-3
"""

import json
import re
import sys
from pathlib import Path

import duckdb
import httpx

from cities import CITY_LIST

BUCKET = "https://ookla-open-data.s3.amazonaws.com"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "ookla"
AIRBNB = ROOT / "data" / "raw" / "airbnb"
OUT = ROOT / "data" / "processed"
BUFFER_DEG = 0.01  # about 1 km


def newest_quarter(client: httpx.Client) -> tuple[int, int]:
    xml = client.get(BUCKET, params={"list-type": "2", "prefix": "parquet/performance/type=fixed/"}).text
    found = re.findall(r"year=(\d{4})/quarter=(\d)/[^<]+\.parquet", xml)
    if not found:
        raise RuntimeError("No Ookla parquet files found on S3")
    return max((int(y), int(q)) for y, q in found)


def download(client: httpx.Client, kind: str, year: int, quarter: int) -> Path:
    month = {1: "01", 2: "04", 3: "07", 4: "10"}[quarter]
    key = f"parquet/performance/type={kind}/year={year}/quarter={quarter}/{year}-{month}-01_performance_{kind}_tiles.parquet"
    dest = RAW / f"{year}-Q{quarter}_{kind}.parquet"
    if dest.exists():
        print(f"{dest.name} already downloaded")
        return dest
    RAW.mkdir(parents=True, exist_ok=True)
    print(f"downloading {kind} tiles for {year} Q{quarter}...", flush=True)
    tmp = dest.with_suffix(".part")
    with client.stream("GET", f"{BUCKET}/{key}") as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.rename(dest)
    return dest


def city_boxes(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    manifest = json.loads((AIRBNB / "manifest.json").read_text())
    boxes = []
    for c in CITY_LIST:
        snap = manifest[c["slug"]]["snapshot_date"]
        path = AIRBNB / c["slug"] / snap / "listings.csv.gz"
        s, n, w, e = con.execute(
            f"""select min(lat), max(lat), min(lon), max(lon) from (
                  select try_cast(latitude as double) lat, try_cast(longitude as double) lon
                  from read_csv('{path}', all_varchar=true, header=true)) where lat is not null and lon is not null"""
        ).fetchone()
        boxes.append((c["city"], s - BUFFER_DEG, n + BUFFER_DEG, w - BUFFER_DEG, e + BUFFER_DEG))
    return boxes


def main(args: list[str]) -> None:
    with httpx.Client(timeout=600, follow_redirects=True) as client:
        if "--quarter" in args:
            y, q = args[args.index("--quarter") + 1].split("-")
            year, quarter = int(y), int(q)
        else:
            year, quarter = newest_quarter(client)
        files = {kind: download(client, kind, year, quarter) for kind in ("fixed", "mobile")}

    con = duckdb.connect()
    boxes = city_boxes(con)
    con.execute("create table boxes(city varchar, s double, n double, w double, e double)")
    con.executemany("insert into boxes values (?, ?, ?, ?, ?)", boxes)

    # Ookla's files carry tile_x / tile_y: the tile's center lon / lat. No geometry needed.
    selects = [
        f"""select b.city, '{kind}' as type, t.quadkey, t.avg_d_kbps, t.avg_u_kbps, t.avg_lat_ms,
                   t.tests, t.devices, t.tile_y as tile_lat, t.tile_x as tile_lon
            from read_parquet('{path}') t
            join boxes b on t.tile_y between b.s and b.n and t.tile_x between b.w and b.e"""
        for kind, path in files.items()
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / "ookla_tiles.parquet"
    con.execute(f"copy ({' union all '.join(selects)}) to '{out}' (format parquet)")
    (OUT / "ookla_meta.json").write_text(json.dumps({"year": year, "quarter": quarter, "buffer_deg": BUFFER_DEG}))

    print(f"\nwrote {out}")
    for row in con.execute(
        f"select city, type, count(*), sum(tests), round(median(avg_d_kbps)/1000, 1) from '{out}' group by all order by city, type"
    ).fetchall():
        print(f"{row[0]:15} {row[1]:6} tiles={row[2]:6} tests={row[3]:8} median_down={row[4]} Mbps")


if __name__ == "__main__":
    main(sys.argv[1:])
