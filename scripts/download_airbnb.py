"""Download each city's Inside Airbnb listings and calendar files, once.

Inside Airbnb asks that the data be downloaded once, not on every run, and not republished.
Files land in data/raw/airbnb/<slug>/<snapshot date>/ (gitignored). A file that's already
there is skipped. data/raw/airbnb/manifest.json records each city's snapshot date and URLs.

    uv run scripts/download_airbnb.py              # every city in cities.py
    uv run scripts/download_airbnb.py lisbon porto # just these slugs
    uv run scripts/download_airbnb.py --dry-run    # print URLs and sizes, download nothing
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import httpx

from cities import CITY_LIST

GET_THE_DATA = "https://insideairbnb.com/get-the-data/"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "airbnb"
FILES = ("listings.csv.gz", "calendar.csv.gz")
# country/region/city-slug/YYYY-MM-DD/data/listings.csv.gz
URL_RE = re.compile(r'https://data\.insideairbnb\.com/([^"/]+)/([^"/]+)/([^"/]+)/(\d{4}-\d{2}-\d{2})/data/listings\.csv\.gz')


def latest_snapshots(client: httpx.Client) -> dict[str, tuple[str, str]]:
    """slug -> (snapshot date, base URL) for the newest snapshot the page lists."""
    html = client.get(GET_THE_DATA).text
    found: dict[str, tuple[str, str]] = {}
    for m in URL_RE.finditer(html):
        slug, date = unquote(m.group(3)), m.group(4)
        base = m.group(0).rsplit("/", 1)[0]
        if slug not in found or date > found[slug][0]:
            found[slug] = (date, base)
    return found


def download(client: httpx.Client, url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", url) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.rename(dest)  # only a finished download gets the real name


def main(args: list[str]) -> None:
    dry_run = "--dry-run" in args
    wanted = [a for a in args if not a.startswith("--")]
    cities = [c for c in CITY_LIST if not wanted or c["slug"] in wanted]

    RAW.mkdir(parents=True, exist_ok=True)
    manifest_path = RAW / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    with httpx.Client(timeout=120, follow_redirects=True) as client:
        snapshots = latest_snapshots(client)
        total = 0
        for c in cities:
            if c["slug"] not in snapshots:
                print(f"!! {c['city']}: not on Inside Airbnb's page, skipping")
                continue
            date, base = snapshots[c["slug"]]
            out = RAW / c["slug"] / date
            for name in FILES:
                url, dest = f"{base}/{name}", out / name
                if dry_run:
                    size = int(client.head(url).headers.get("content-length", 0))
                    total += size
                    print(f"{c['city']:15} {date} {name:16} {size / 1e6:7.1f} MB")
                    continue
                if dest.exists():
                    print(f"{c['city']:15} {date} {name:16} already downloaded")
                    continue
                out.mkdir(parents=True, exist_ok=True)
                print(f"{c['city']:15} {date} {name:16} downloading...", flush=True)
                download(client, url, dest)
            if not dry_run:
                manifest[c["slug"]] = {"city": c["city"], "snapshot_date": date, "base_url": base}
                manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        if dry_run:
            print(f"\nTotal: {total / 1e6:.0f} MB compressed")


if __name__ == "__main__":
    main(sys.argv[1:])
