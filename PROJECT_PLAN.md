# Project Plan: NomadReady (Deep Research Data Exploration Agent)

This file is the full context for this project. Read it before writing code. It covers what the class assignment requires, the project idea, the data, the agent design, and the deliverables. Solo project.

---

## 1. Assignment requirements (from the course spec)

**Project 2: A Data Exploration Agent.** Build an agent that explores a dataset to answer questions people actually have, and deploy it to Google Cloud Run. Path chosen: **Deep Research** (`"path": "research"`).

### The agent must
- Use data the model doesn't already know deeply.
- Get at the data through tools (SQL, files, search, or an API).
- Combine evidence into a supported claim.
- State when the data can't answer a question.
- Show its work. **Keep the `/chat` response shape from Project 1.** Sub-agent tool calls go in `tool_calls` too, each with an `agent` field.
- Have a front end that says what the agent explores, who it's for, and how to use it.
- Be deployed to GCP Cloud Run.

### Deep Research path items (graded, 7 pts)
- At least **three agent shapes with different jobs** (Lead, Researcher, Writer).
- The Lead **fans out to several Researchers in one turn**; they **run in parallel in fresh contexts**.
- The Lead **goes back for more when findings leave a gap**, up to a turn cap.
- **Vague input questions require clarification.**
- The output is **accurately cited**.

### Reference starter code
- The course provides `wikipedia-deep-research/` as the reference for this path (unzipped in the repo folder). Follow its structure for how sub-agents are spawned. **It has no `/chat` endpoint** (it streams over `/run`).
- The `/chat` request/response shape comes from the Project 1 starter, `gemini-web-tool-calling/`. Match it exactly. Both starters are summarized in **section 12**. Don't invent a new response shape.

### Grading
| Section | Pts | What's assessed |
|---|---|---|
| Basics | 1 | README + valid `submission.json` |
| Functionality | 9 | Deployed and reachable; clear purpose; answers its research questions correctly |
| Data | 4 | Unique dataset/problem; data the model doesn't know; admits what the data can't answer |
| Path | 7 | The deep research items above |
| Creativity | 4 | Frontend changed from the starter; the agent does something interesting |

Team-size rule: at least one data source per team member. This is a solo project, so one source is the minimum. We use three core sources plus helpers.

### Repo root must contain
- `app.py`, `pyproject.toml`, `uv.lock` (use `uv` for dependency management)
- `README.md`
- `submission.json`:
```json
{
  "deploy_url": "https://<service>.run.app",
  "authors": ["<Christina's UNI>"],
  "path": "research"
}
```

### README must include
1. A description of the project and its target user.
2. Where the data came from and how someone could load it (the grader doesn't need to load it, just understand it).
3. **Three sample queries.** For each one, give:
   - the expected answer,
   - the file, table, or API endpoint it comes from,
   - the answer from the **same model with no tools and no system prompt**, next to the agent's answer.

### Deployment requirements
- Cloud Run with **continuous deploy from GitHub** (Cloud Build trigger on push).
- Ship the data with the deploy. Commit it if each file is under 100 MB; otherwise load it from a **GCS bucket at startup**. For this project, use GCS (see the Inside Airbnb terms in section 3).
- Graders wait **up to 5 minutes per answer**. Raise the Cloud Run request timeout (e.g., 600s) and **cap the agent's max_turns** so answers finish well within 5 minutes.
- Keep the service running until grades are released.
- Repo can be private. Add these GitHub collaborators: `codeboi07`, `bhuvighosh3`, `nniishhh`, `x`.

---

## 2. The idea

**NomadReady** helps remote workers decide where to spend a few weeks to a few months working remotely, and *where within each city* to stay. Stays can be as short as **7 nights**, so split trips (e.g., two weeks in Lisbon, then two in Porto) are supported.

**Target user:** remote workers and students on breaks planning stays of a week or longer. They have hard constraints: a work schedule that must overlap a home time zone, a budget, and a need for reliable internet. Generic "best cities for digital nomads" lists ignore those constraints.

**Core question:** "Given my dates (or month and stay length), budget, and required online hours, which of these cities work, and which neighborhoods in each should I stay in?"

**Creative hook:** a spatial join of Airbnb listing locations to Ookla internet speed tiles. This gives *neighborhood-level measured internet speed near actually rentable month-long listings*. No model or travel blog knows this.

---

## 3. Data sources

### Core sources (pre-processed offline, loaded from GCS at startup)

**1. Inside Airbnb: listings and calendar files**
- Source: https://insideairbnb.com/get-the-data/
- Per city: `listings.csv.gz` (detailed) and `calendar.csv.gz`. Free downloads cover quarterly snapshots for the last ~12 months. **Downloaded:** September 2026 snapshots (2026-09-14 to 2026-09-28) for all 20 cities, 608 MB; dates in `data/raw/airbnb/manifest.json`.
- Useful fields: `id`, `latitude`, `longitude`, `neighbourhood_cleansed`, `room_type`, `accommodates`, `bedrooms`, `price`, `minimum_nights`, `maximum_nights`, `amenities` (look for "Dedicated workspace" and "Wifi"), `review_scores_rating`, `number_of_reviews`. Calendar: `listing_id`, `date`, `available`, `minimum_nights`, `maximum_nights`.
- **The calendar has no `price` column** (Inside Airbnb dropped it). The only price is the listing's single nightly `price` as of the snapshot. The calendar is used only for availability.
- **Terms: do not republish the data, and download it once rather than on every run.** So **don't commit the raw or processed Airbnb data to a public repo.** Keep the repo private and/or store the processed DB in a GCS bucket. Credit Inside Airbnb in the README.
- `price` is in **local currency even though it's written with a "$"** (e.g., Buenos Aires 186,530 = ARS). Currencies per city are in `scripts/cities.py`. Record the snapshot date per city, because every citation must include it.
- Stay price estimate: the listing's nightly `price` × nights, converted to USD. Be explicit that this is one snapshot nightly rate, not a live quote, and that it **doesn't reflect weekly or monthly discounts** many hosts give (so long-stay estimates likely run high).
- **Bonus field:** ~23K listings (6%) carry Airbnb's own Wi-Fi speed test in `amenities` (e.g., "Fast wifi – 208 Mbps"). Stored as `wifi_mbps_listed`: a unit-level speed to show next to Ookla's area-level speed when present.
- Neighborhoods are whatever `neighbourhood_cleansed` holds, and granularity varies by city. In Mexico City they're boroughs (alcaldías: Cuauhtémoc, Miguel Hidalgo), not colonias like Roma or Condesa.
- **Neighborhood name fixes** (`scripts/neighbourhood_names.py`, applied in `build_db.py`): Inside Airbnb's Lisbon names drop every accented letter ("Belm", "Parque das Naes"), so 45 are restored. Athens (Greek capitals) and Taipei (Chinese) get English names. The original is kept in `neighbourhood_local`, and tools match either name, ignoring case and accents.
- `neighbourhood_group` (municipality/district) is filled in for Lisbon, Porto, Barcelona, Madrid and Berlin. Lisbon's and Porto's data covers the **whole metro area** (Sintra, Cascais, Vila do Conde…). Pass `neighbourhood="Lisboa"` to `find_stays` for the city proper.

**2. Ookla Speedtest open data: fixed and mobile performance tiles**
- Source: `s3://ookla-open-data/parquet/performance/type={fixed|mobile}/year=YYYY/quarter=Q/` (anonymous access: `aws s3 cp ... --no-sign-request`, or HTTPS at `https://ookla-open-data.s3.amazonaws.com/...`).
- Global parquet files of tiles about 600m across, at zoom 16. Fields include `quadkey`, `tile` (WKT polygon), `avg_d_kbps`, `avg_u_kbps`, `avg_lat_ms`, `tests`, `devices`.
- Use the most recent available quarter. **Filter offline to each city's bounding box** so the deployed data is small. **Done:** 2026 Q3; each box is the city's listing extent + 0.01° (~1 km); 126K tiles in `data/processed/ookla_tiles.parquet`.
- Join each Airbnb listing to its tile by computing the listing's zoom-16 quadkey from lat/lon, so no geometry ops are needed at runtime. (Computed with a SQL macro in `build_db.py`, checked against `mercantile`.)
- **Measured join coverage:** 97–100% of listings per city fall in a fixed tile; 89–100% in a tile with ≥10 tests; 99–100% when the 8 neighbor tiles are included. Lowest single-tile confidence: Berlin, Lisbon, Porto.
- Treat tiles with few `tests` as low confidence, and have the agent say so.
- Cite as: "Speedtest® by Ookla® Global Fixed and Mobile Network Performance Maps, Q_ YYYY."

**3. Open-Meteo historical weather API (fetched at build time, stored in the `climate` table)**
- Endpoint: `https://archive-api.open-meteo.com/v1/archive`. Free, no API key. Data is CC BY 4.0, so storing it is allowed (attribution in the citation).
- At each city's center, 5 complete years (2021–2025) of daily max/min temperature, precipitation, sunshine and daylight, averaged into 12 monthly rows per city (240 rows) by `scripts/build_climate.py`. `build_db.py` reloads them from `data/processed/climate.parquet` on a rebuild.
- **Why not live:** Open-Meteo weights requests by size. One 5-year daily request reports `X-RateLimit-Cost: 65.2` (about 130 two-week chunks × 5 of 10 variables), so the free tier's 600 calls/minute, 5,000/hour and 10,000/day are only ~9, ~76 and ~153 of our requests. Live calls hit 429s in testing (in a 3-city run, Lisbon and Mexico City came back without weather). Cloud Run also shares outbound IPs with other apps, and the limits are per IP. The one-time build costs ~1,300 calls (20 requests spaced 7 s apart) and returned identical values to the live tool.

### Helper sources (live calls; not counted as core)
- **U.S. State Department travel advisories:** `https://cadataapi.state.gov/api/TravelAdvisories`. No auth. Returns advisory level 1–4 per country. Mention the level and link; don't editorialize.
- **Exchange rates:** Frankfurter (`https://api.frankfurter.app/latest?from=USD`, ECB data, no key). The ECB doesn't publish **ARS, CLP or TWD**, so those come from **open.er-api.com** (ExchangeRate-API, free, no key). Rates are **fetched once at build time** into the `fx_rates` table (with source and as-of date) so SQL can compute USD prices; the snapshot prices are from September anyway. For Buenos Aires, note that the official ARS rate can differ a lot from what visitors actually pay.
- **Work-hours overlap:** computed locally with Python `zoneinfo`. It's a tool, not a data source.

### City scope (decided: 20 cities, in `scripts/cities.py`)
- **Europe:** Lisbon, Porto, Barcelona, Madrid, Rome, Athens, Berlin, Amsterdam, Prague, Istanbul
- **Americas:** Mexico City, Buenos Aires, Rio de Janeiro, Santiago, Montreal, Vancouver
- **Asia:** Bangkok, Tokyo, Taipei
- **Africa:** Cape Town

Medellín was dropped because Inside Airbnb doesn't cover it. Tokyo, Taipei and Santiago were added for time-zone spread.

---

## 4. Offline data pipeline (`scripts/`)

Status: steps 1–3 are **done**. Run order: `uv run scripts/download_airbnb.py`, `uv run scripts/download_ookla.py`, `uv run scripts/build_db.py`, then `uv run scripts/build_climate.py` (first time only; it caches to `data/processed/climate.parquet`, which later `build_db.py` runs reload). City config is in `scripts/cities.py`.

1. `download_airbnb.py`: download listings and calendar files for the chosen cities **once** and store them locally (gitignored). Skips files already downloaded; `--dry-run` prints sizes.
2. `download_ookla.py`: download the latest quarter's fixed and mobile parquet files (~555 MB, once) and filter them to the city bounding boxes.
3. `build_db.py`: build one **DuckDB** file (`data/nomad.duckdb`, 152 MB, ~25 s) with these tables:
   - `cities(city, slug, country, iso2, timezone, currency, airbnb_snapshot_date, ookla_quarter, center_lat, center_lon)`. 20 rows; the center (median listing location) is for weather lookups.
   - `listings(listing_id, city, neighbourhood, name, listing_url, lat, lon, room_type, accommodates, bedrooms, beds, price_local, minimum_nights, maximum_nights, has_workspace, has_wifi, has_ethernet, wifi_mbps_listed, rating, n_reviews, last_review, quadkey)`. 385K rows: listings with a price, a location, and at least one open stretch of 7+ nights.
   - `availability(listing_id, city, start_date, end_date, nights, min_nights_max)`. 950K rows. **One row per unbroken stretch of open nights** (inclusive dates), keeping stretches of **7+ nights**. Built from the ~160M-row daily calendar with gaps-and-islands. `min_nights_max` is the highest minimum stay the host set within the stretch, so we never claim a booking the host wouldn't accept. A stay from A to B fits if `start_date <= A`, `end_date >= B - 1 day`, nights ≥ `min_nights_max`, and nights ≤ the listing's `maximum_nights`. Availability is as of the snapshot; some dates have been booked since.
   - `speed_tiles(city, type, quadkey, avg_d_mbps, avg_u_mbps, avg_lat_ms, tests, devices, tile_lat, tile_lon)`. 126K rows; join on `(city, quadkey)`.
   - `fx_rates(currency, usd_per_unit, source, as_of)`. 12 rows.
   - view `neighbourhood_summary(city, neighbourhood, n_listings, n_with_workspace, median_nightly_local, median_nightly_usd, median_d_mbps_fixed, share_low_confidence)`. Not tied to dates; stay-specific numbers are computed by the tools at query time.
   - `build_info(built_on, min_stay_nights, low_confidence_tests)`. Low confidence means fewer than 10 Ookla tests in the tile.
4. Upload `nomad.duckdb` to a GCS bucket. In `app.py`, download it at startup (or lazily on first request) using the Cloud Run service account.

Gitignore all raw and processed data.

---

## 5. Agent architecture

Follow the reference starter's structure for how agents are spawned and how tool calls are recorded.

**Built (step 4)** in `scout/`. It isn't called `agents/` because that would shadow the OpenAI Agents SDK package.
- **Flow (`scout/pipeline.py`):** `intake` → Lead fan-out and gap loop → Writer → Verifier → (one Writer revision if needed) → Sources appended in code.
- **Models:** `vertex_ai/gemini-3.5-flash` for every agent. Researchers were on flash-lite first, but in testing it garbled a count (1,121 for a tool's 1,799) while copying, so they now use flash too.
- **`/chat` (`app.py`):** keeps the Project 1 shape. `tool_calls` entries are `{agent, name, args, result}`, recorded by `RecordingHooks.on_tool_end` (`scout/state.py`), which gets the agent, `tool_arguments` and the result from the SDK. Agents are `Lead`, `Researcher-<City>` (`#2`, `#3` for follow-ups), `Writer` and `Verifier`. Sessions are an in-memory `session_id` → conversation dict; `/clear` drops one. `static/index.html` is a placeholder (the Project 1 page) until step 5.
- **Tests:** `tests/test_scout.py` has offline tests for the gate rules, budget math, citation parsing, the Verifier's number check, reconciliation and the hooks; `tests/test_run.py` covers `/run`'s event stream (pipeline stubbed), sessions, the busy guard, progress lines and the query cap. 73 offline tests pass.
- **Measured times:** single city (Q2, Q3) 76–84 s; 3 cities (Q1) 81–87 s; 4 cities after a clarification 154 s; tight budget with 2 follow-up rounds and 5 researchers 184 s. The 120 s research deadline was added after that run.

### Lead (planner)
- **Clarification gate (as built, `scout/intake.py`).** The Lead's first phase is a structured-output call that only *extracts* what the user said across the whole conversation. Code then decides, so the gate is predictable:
  - **No covered city named:** dates (or a month plus a stay length), a USD budget, and work hours/home time zone (or "flexible") are all required. Otherwise ask one clarifying question listing what's missing, with an example. "Somewhere warm and cheap" gets asked.
  - **A covered city named:** a rough time (dates, a month, or a season) and a rough length are still required, because prices, availability and weather all depend on them and guessing would research a different trip. Budget and work hours may be left out: they're filled with *stated* defaults (no price filter, overlap not checked) listed in `brief.assumptions` for the Writer to state. The question names the city and only asks for timing and length.
  - **Seasons** map to their middle month in northern-hemisphere terms (summer = July), recorded as an assumption so the user can correct it. Rough lengths are converted by the extractor ("a few weeks" = 21 nights, "a couple of months" = 60).
  - **Only uncovered cities named** (e.g., Medellín): treated like no city. If the brief is complete, the Lead picks comparable covered cities and the Writer says Medellín isn't covered.
  - **Off-topic:** a short intro to what NomadReady does.
  - **Code does the arithmetic:** dates resolve to the next occurrence of the month, and budgets convert to a stay total ("$2,500/month" for 28 nights = $2,500; per-week and per-night budgets are scaled). "1BR" means `room_type = Entire home/apt`. Stays under 7 nights, dates outside the calendar window, and non-USD budgets are asked about. `wants_coworking` is set when the user asks about coworking.
- **Fan-out.** Spawn **one City Researcher per candidate city, in parallel, in one turn**, each in a fresh context with a structured brief (city, month, budget, workspace need, work hours, home timezone).
- **Gap check.** Read the Researchers' findings and decide whether there's a gap. If so, send targeted follow-ups, up to a **round cap of 2–3 follow-up rounds**. Example gaps:
  - Fewer than ~5 qualifying listings under budget, so widen the neighborhoods, suggest splitting the stay across two cities or neighborhoods, or report scarcity.
  - Speed tiles near the cheap listings have low test counts, so widen the radius and flag low confidence.
  - Work-hours overlap is poor for all candidates, so add a city in a closer time zone.
  - A Researcher's tool call failed or returned empty, so retry once or record it as unknown.
  - **No dedicated workspace:** a recommended stay is a shared room, or most recommended listings lack `has_workspace`. Then send a coworking follow-up (`nearby_coworking`) for that city's recommended neighbourhood(s). **Code cap: at most 8 coworking searches per question**, enforced in the run context like the starter's `MAX_RESEARCHERS`. A Google Cloud console quota of 300 Text Search requests/day is the backstop.
- **Out-of-scope detection.** Identify parts of the question the data can't answer and pass them to the Writer.

### City Researcher (fresh context per city)
Tools (**built and tested**: `tools/`, 54 tests in `tests/test_tools.py`; `uv run pytest`, or `-m "not network"` to skip live APIs). Each returns a JSON string with a `citations` list (`id`, `source`, `detail`, `url`) or `{"error": ...}`; none raise.
- `find_stays(city, check_in, check_out, max_total_usd?, needs_workspace?, room_type?, min_bedrooms?, guests?, neighbourhood?)`: **the main tool.** Listings open every night of the stay whose host accepts that length, priced in USD for the whole stay, grouped by neighbourhood with Ookla speeds, plus 5 example listings with URLs. Also returns counts within and outside the budget and p10/median/cheapest totals, so gaps are visible. **Defaults to listings with ≤2 bedrooms** (`max_bedrooms=2`; studios with 0 or unknown bedrooms are kept; `None` lifts the cap). The cap removes ~12% of listings and lowers city medians 3–17% (most in Barcelona, Cape Town, Vancouver, Taipei). The rows stay in the DB, so `query_listings` and `neighbourhood_summary` still include big homes unless they filter. **All room types are included**, shared rooms/dorm beds too, for budget travelers. Only prices under $1/night (2 listings) are dropped as broken. The 875 listings at $1–10/night are real rooms and dorm beds, mostly in Istanbul, Bangkok, Rio and Taipei. Added because the open-stretch date logic is easy for a model to get wrong in raw SQL. A test re-checks returned listings against the raw daily calendar.
- `query_listings(sql)`: read-only SQL for follow-up questions. Exactly one SELECT, max 50 rows, 15 s timeout. The connection has `enable_external_access=false` (locked), so SQL can't read files or URLs.
- `neighbourhood_speed(city, neighbourhood?)`: per-neighbourhood fixed/mobile medians, latency, low-confidence share, and Airbnb Wi-Fi test medians. For one neighbourhood, it adds a widened 3×3-tile estimate.
- `month_climate(city, month)`: reads the `climate` table (Open-Meteo archive at the city's center, averaged over 2021–2025). Takes a city instead of lat/lon. No live call.
- `travel_advisory(city)`: State Department API. **The API uses FIPS 10-4 country codes, not ISO** (Chile = CI, Czechia = EZ, Japan = JA); mapped in `tools/advisory.py`. Cached for 1 hour.
- `fx_to_usd(currency)`: reads the `fx_rates` table (Frankfurter, or open.er-api.com for ARS/CLP/TWD), and returns the source and as-of date for citation.
- `nearby_coworking(city, neighbourhood?)`: **Google Places API (New), live** (see section 11). Text search "coworking space" within ~1.5 km of the median location of the neighbourhood's listings. Returns up to 10 places with rating, rating count, distance, hours, website and Maps link, plus `n_coworking` and `n_well_rated` (4.5+ stars from 20+ ratings). `is_coworking` uses Google's own `coworking_space` category, which filters out offices and cafés that match by name. Nothing is cached, per Google's terms. Without `GOOGLE_PLACES_API_KEY` it returns "coworking unknown". Called only for stays without a dedicated workspace.
- `work_overlap(city, home_tz, work_start, work_end, on_date?)`: zoneinfo. Gives the local work window and the hours outside 08:00–22:00 local. Uses the stay date so DST is handled (e.g., in mid-March the US has switched and Europe hasn't). Accepts "Eastern", "PT", "9am", etc.

Returns **structured JSON** findings: `CityFindings` (fit, summary, counts, cost, work hours, weather, advisory, 1–3 `NeighbourhoodFinding`s with example listing URLs, `facts` each with `cites`, and `gaps`), via the SDK's `output_type`.
- **Reconciliation (`scout/lead.py: reconcile`):** after each Researcher finishes, code checks its neighbourhood numbers (listings in budget, median total, median speed) against its *own* `find_stays` results. A number is kept if it matches any of them, since a researcher may call with different filters. A number matching none is replaced with the row from the call that used the brief's budget. Missing example links are filled in. Corrections are logged in `state.corrections`.

### Writer
- Ranks the cities against the user's constraints and recommends 1–3 neighborhoods per city, with a table per city (adds a coworking column only if searched; "Listings available" when there's no budget). Leads cost with median and p10, not the single cheapest listing.
- **Every number has an inline citation id**, e.g. `$2,149 [airbnb-lisbon]`, `214 Mbps [ookla-fixed-2026-Q3]`, `[open-meteo-lisbon-02]`, `[fx-EUR]`, `[state-dept-pt]`, `[google-places-lisbon-arroios]`, `[iana-tz]`. Every id contains a dash, which is how `cited_ids` (and the page) tell citations from other bracketed text; the overlap tool's id was `zoneinfo` until the page showed it was being dropped from Sources.
- **The Sources section is built in code** from the citations the tools actually returned (collected by the hooks), listing only the ids the report uses. Ids no tool returned trigger a revision, and are marked `[unverified]` if they survive it.
- Includes a **"What this data can't tell you"** section for anything out of scope.
- States that prices are snapshot estimates, not live quotes.

### Verifier (built: the fourth agent shape, `scout/verify.py`)
- **Code step:** finds every sentence or table row (with its column headers and city section) in the draft containing a number that no data tool returned. Rounding, percentages and the brief's own numbers are allowed.
- **Verifier agent step** (only if something is flagged): judges each flagged item against the raw tool results as `derived_ok`, `wrong` (with the corrected sentence) or `unsupported`. Anything wrong or unsupported goes back to the Writer for **one** revision.
- **Tested:** on a draft with three garbled Buenos Aires counts (1,121 / 454 / 405) it corrected all three (1,799 / 761 / 558). Splitting table rows into single cells lost the context and gave wrong corrections, so rows are passed whole.

### Limits and timeouts (`scout/config.py`)
- `MAX_RESEARCHERS = 10` per question.
- `MAX_ROUNDS = 3`: Lead turns that spawn researchers (1 fan-out + 2 follow-up rounds), counted by the hooks.
- `MAX_COWORKING = 8` Places searches per question.
- `RESEARCH_DEADLINE_S = 120`: no new researchers after this.
- Lead `max_turns = 10`, Researcher `max_turns = 12`. Model calls time out at 90 s with 2 retries.
- `MAX_QUERIES_PER_RESEARCHER = 3` custom SQL calls, counted per researcher via the tool context's agent. Added after a Lisbon researcher spent all 12 turns on `query_listings` and returned nothing (~70 s lost, plus a re-sent researcher).
- **Out of turns:** a `max_turns` error handler gives the researcher one more turn with `tool_choice="none"` to return findings from what it already has; `gaps` then says it ran out of turns. Tested with the cap forced to 4: Porto came back with real numbers and gaps naming what it skipped (+15 s).
- A failed researcher becomes a gap, not a crash.
- Cloud Run request timeout: 600 s.
- Parallel Researchers: the Lead calls `research_city` several times in one turn and the SDK runs them concurrently, as in the starter. The data tools are sync and run in threads.

---

## 6. What the agent must explicitly say it can't answer
- Visa rules and how long someone can legally stay.
- Coworking beyond what Google Maps shows: day-pass or desk prices, desk availability, community or events. The agent can give locations, ratings, hours and websites (for stays without a workspace) but not whether a space is good for you.
- Neighborhood-level safety, noise, or "vibe."
- Cities Inside Airbnb doesn't cover.
- Live or current prices (the data is a quarterly snapshot).
- A specific listing's Wi-Fi, except for the ~6% of listings showing Airbnb's own speed test (`wifi_mbps_listed`). Otherwise Ookla gives the surrounding area's measured speeds, not the unit's.
- Weekly/monthly discounts and the real all-in price (estimates use the snapshot nightly rate × nights).
- The rate visitors actually get when exchanging ARS (we use the official rate).
- Areas finer than the city's `neighbourhood_cleansed` field (e.g., Roma vs Condesa inside Mexico City's Cuauhtémoc borough).
- Non-Airbnb housing (leases, sublets, hotels).

---

## 7. Frontend

It must be changed from the starter and must explain what the agent explores, who it's for, and how to use it.
- Header and intro: "NomadReady: find where to work remotely for a month, and which neighborhood to stay in." Include the target user, the data sources and their snapshot dates, and what it can't answer.
- Input: a short constraint form (dates or month + stay length, budget USD for the stay, work hours + home time zone, needs a dedicated workspace, optional candidate cities) **plus** a free-text box. Show clarifying questions as a chat turn.
- Output: one **card per city** (verdict, estimated monthly cost, overlap hours, typical weather, advisory level) with a small **neighborhood table** (neighborhood, number of qualifying listings, median monthly price, median download speed, confidence).
- An expandable **"How I got this"** panel rendering `tool_calls` grouped by `agent` (Lead / Researcher-Lisbon / Writer / Verifier).
- A Sources list at the bottom.
- **Live progress via `POST /run`** (built): same body and sessions as `/chat`, streamed as server-sent events (read with `fetch` + a stream reader, since `EventSource` can't POST). Events: `session`; then `phase` (intake / research / writing / checking / revising), `brief`, `agent` (started / finished), `step` (a tool call starting, then a readable line of what it found, matched by tool call id, from `scout/progress.py`), `tool_call` (the full `/chat` record), `checked` (the Verifier's problems); then `done` {response, clarifying, n_tool_calls, t} or `error`. Every event carries `t` (seconds). A `: ping` every 15 s keeps proxies from closing a quiet stream. If the page disconnects, the run is cancelled and the question is dropped from the session. A session with a question in progress refuses a second one (both endpoints).
- **Tested live:** Lisbon vs Porto, 3 weeks in April: both researchers start at 9.4 s, finish at 41 s and 53 s, report done at 143 s with 20 tool calls. A curl disconnect at 20 s left the session free and its history clean.

**As built (`static/index.html`, one file, no build step):**
- `GET /about` supplies the covered cities, snapshot dates, Ookla quarter and bookable window, used for the city chips, the month list (only months with room for a 7-night stay) and the data line in the intro.
- The trip form is turned into a plain sentence ("I'm considering Lisbon and Porto. I'll stay for 3 weeks, starting in April 2027. …") and sent like typed text, so the form and free text go through the same intake. The form resets and collapses after sending; follow-up answers are typed.
- While running: a stage stepper (Understanding → Researching → Writing → Fact-checking) with a timer, and one lane per agent (Lead full width, then Researchers, Writer, Verifier) with each tool step's live line; a "data" toggle shows that call's arguments and result.
- The report is split at its `##` headings into a summary card, one card per city (name + verdict), and a "What this data can't tell you" card. Tables scroll sideways on phones. `[citation-id]` markers become numbered chips linking to the Sources list, which is built from the `done` event's structured `sources`, not the markdown. Unknown ids show as a warning "?" chip. Google Maps attribution appears whenever coworking was searched. "Copy as Markdown" copies the full report.
- After the answer, the lanes collapse into "How I got this · N agents · N tool calls · time".
- Markdown is rendered with marked 12.0.2 and sanitized with DOMPurify 3.1.6 (cdnjs); if either fails to load, the report is shown as plain text, never unsanitized HTML. Light and dark themes; no horizontal scroll at 390 px.
- **Tested in headless Chrome:** vague question → clarifying question → form answer in the same session (92 s, 2 city cards, 44 citations all resolving, attribution shown); the 3-city sample (156 s, 6 agents, 37 tool calls, 64 citations, 0 unresolved); no console errors.
- **Found by the page:** the work-hours citation id `zoneinfo` had no dash, so it was never recognised as a citation and was missing from Sources since step 4. Renamed to `iana-tz`, with a test.

---

## 8. Sample queries for the README (fill in expected answers after the data is built)

Each one gives a rough time and length so it runs in one turn (see the gate rules in section 5); the vague query below shows the clarification.

1. **Full comparison:** "A month in February, $2,500/month for a 1BR with a dedicated workspace, online 9am–5pm Eastern. Compare Lisbon, Mexico City, and Buenos Aires."
   Sources: `listings`, `calendar`, `speed_tiles`, Open-Meteo archive, State Dept API.
2. **Neighborhood drill-down (shows off the spatial join):** "In Mexico City, which neighborhoods have month-long rentals in March under $1,500 and the fastest home internet?"
   Sources: `listings` ⨝ `speed_tiles` on quadkey. Expect the no-tools baseline to guess generically (e.g., "Roma/Condesa have good Wi-Fi") with no numbers.
3. **Partially answerable:** "Can I stay in Lisbon for 6 months starting in January, and is the coworking scene good?"
   The agent answers housing, internet, and weather from the data, lists nearby coworking spaces with Google ratings, and states clearly that visa rules, and coworking prices or community, are outside its data.

For each query: run it through the agent, run it through the same model with no tools and no system prompt, and put both answers side by side in the README, along with the expected answer and the file, table, or endpoint it comes from.

Also test a **vague query** (e.g., "Where should I go somewhere warm and cheap?") to show the clarification behavior.

---

## 9. Repo layout (as built)
```
app.py                 # FastAPI: /chat, /clear, / (static/index.html)
scout/                 # agents (not agents/: that shadows the SDK)
  config.py            #   models + caps
  state.py             #   Research run state + RecordingHooks (tool_calls, citations)
  intake.py            #   Lead phase 1: extract brief, clarification gate
  lead.py              #   Lead phase 2: research_city fan-out, gap loop, reconcile
  researcher.py        #   City Researcher: tool wrappers + CityFindings
  writer.py            #   Writer + citation check + Sources
  verify.py            #   Verifier
  pipeline.py          #   answer(conversation) -> (response, state)
  progress.py          #   readable progress lines for /run's step events
tools/                 # db.py, stays.py, weather.py, advisory.py, fx.py, overlap.py, coworking.py
tests/                 # test_tools.py, test_scout.py, test_run.py (uv run pytest)
scripts/               # offline pipeline (section 4)
static/                # frontend
data/                  # gitignored
README.md
submission.json
pyproject.toml, uv.lock
Dockerfile             # if the starter uses one
```

## 10. Build order
1. ~~Read the reference starter (`wikipedia-deep-research/`) and the Project 1 `/chat` shape (`gemini-web-tool-calling/`).~~ Done; see section 12.
2. ~~Build the offline pipeline for all 20 cities.~~ Done; see section 4.
3. ~~Implement the tools and test them directly.~~ Done; see section 5 (City Researcher).
4. ~~Build the Lead → parallel Researchers → Writer flow, then add the gap loop and the clarification gate.~~ Done, plus the Verifier; see section 5.
5. ~~Build the frontend.~~ Done; see section 7. Testing it surfaced Open-Meteo 429s; fixed by precomputing climate into the DB (section 3, source 3).
6. Set up the GCS bucket, Cloud Run with continuous deploy from GitHub, a 600s timeout, and a startup data download.
7. Run the sample queries plus the no-tools baselines, and write the README and `submission.json`.
8. Add the grader collaborators to the private repo.

---

## 11. Coworking data: Google Places API (built Nov 2026)

**Decision:** Christina asked for coworking research when a stay has no dedicated workspace (e.g., a shared room). We use the **Google Places API (New)**, live, in `tools/coworking.py` (`nearby_coworking`; see section 5).

**Why Places, not OSM:** measured OSM coverage across city cores (Nov 2026) was thin exactly in the budget cities where shared rooms matter: Rio 4, Athens 6, Cape Town 6, Mexico City 7, Buenos Aires 8 (Berlin 142, Porto 74, Barcelona 54, Madrid 44, Lisbon 34). OSM also included non-coworking noise via the name regex. Places returned 20 (the per-request cap) within 1.5 km of central Mexico City and 15 coworking spaces near Palermo, Buenos Aires (11 rated 4.5+ from 20+ reviews), with ratings, hours and Google's own category.

**Setup (done):**
- GCP project `agentic-ch3991-509700`. `places.googleapis.com` and `apikeys.googleapis.com` are enabled.
- API key `nomadready-places`, restricted to `places.googleapis.com`. Stored in `.env` as `GOOGLE_PLACES_API_KEY` (gitignored, mode 600); `.env.example` has the name only.
- Console quota: **300 Text Search requests/day.** Expected use is 0–8 searches per question.
- **For Cloud Run:** put the key in Secret Manager and mount it as the `GOOGLE_PLACES_API_KEY` env var. Never put it in the repo or the image.

**Terms:** don't store Places content (no table, no cache). Show Google attribution ("Google Maps") wherever results are displayed in the frontend.

**Other options evaluated (not used)**
- **OpenStreetMap via Overpass API.** Free and ODbL-licensed (attribution required); a possible fallback if the Places key is ever unavailable. Query:
  ```
  [out:json][timeout:60];
  (
    nwr["amenity"="coworking_space"](S,W,N,E);
    nwr["office"="coworking"](S,W,N,E);
    nwr["name"~"cowork",i](S,W,N,E);
  );
  out center tags;
  ```
  Run it once per city in the offline pipeline, using the same city bounding boxes as the Ookla filter. The name regex catches spaces that are mapped without the coworking tag.
  - **Why not used:** coverage is incomplete (see the counts above). Earlier spot checks in Nov 2026 found about 70 in Tokyo, 50 in NYC, 24 in Bangkok, 7 in Mexico City and 5 in Medellín, all well below reality. A bounding box instead of the named area didn't change Mexico City's count.
  - **If added:** use it only as a presence signal, as "at least N known coworking spaces within 1 km (OpenStreetMap; likely incomplete)." Never use it as a ranking factor, and flag sparse cities explicitly.
- **CoworkingView API** (`https://api.coworkingview.com`; price index at `https://coworkingview.com/api/price-index`). No key, CC BY 4.0, 120 requests/min, server-side only.
  - **What it gives:** monthly desk price medians by city and type (`/v1/market/rates`) and space locations (`/v1/properties/map`).
  - **Why not used:** coverage is thin and Europe/UK/UAE-heavy, mostly chains. Only Berlin, Barcelona and Amsterdam had real depth when checked. It has no reviews or ratings.
  - **If added:** use it only for desk-price estimates where the city sample is 5 or more. Otherwise report the "entry from" price with a low-confidence flag.
- **coworker.com (including Apify scrapers): rejected.** Its Acceptable Use Policy (§2.2.3) prohibits scraping and data mining without written consent. Do not use.

---

## 12. What the starters give us (read Nov 2026)

**The `/chat` contract (from `gemini-web-tool-calling/app.py`, Project 1). Keep it exactly:**
```
POST /chat
request:  {"message": str, "session_id": str | null}
response: {"response": str, "session_id": str,
           "tool_calls": [{"name": str, "args": dict, "result": str}, ...]}
```
- The only addition for this project: each `tool_calls` entry also gets `"agent"` (e.g., `"Lead"`, `"Researcher-Lisbon"`, `"Writer"`, `"Verifier"`). `result` stays a string (JSON-encoded tool output).
- Sessions live in an in-memory `session_id` → messages dict. This is what lets a clarifying question and the user's answer reach the Lead together. Also keep `POST /clear?session_id=`.
- Errors are returned inside `response` text, not as a 500. Tool errors are returned to the model as `{"error": ...}` JSON so it can reason about them.

**The deep-research pattern (from `wikipedia-deep-research/app.py`):**
- OpenAI Agents SDK (`openai-agents[litellm]`) on Vertex AI via LiteLLM: `vertex_ai/gemini-3.5-flash` for the Lead and Writer, `vertex_ai/gemini-3.5-flash-lite` for Researchers. `ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2})`. Uses gcloud credentials, no API key. `set_tracing_disabled(True)`.
- A `Research` dataclass is passed as the run `context`. It's shared by all agents and tools but never seen by the model, and holds the researcher count and collected findings.
- Fan-out: the Lead has a `research(...)` function tool that clones the Researcher (`researcher.clone(name=f"Researcher {n}")`) and runs `Runner.run(agent, prompt, context=..., hooks=..., max_turns=12)` in a fresh context. The Lead is prompted to make all its calls in one turn, and the SDK runs them in parallel. A hard cap (`MAX_RESEARCHERS`) is enforced in code. For us: `research_city(brief)` cloned as `Researcher-<City>`.
- The Writer is a separate `Runner.run` after the Lead finishes, seeing only the question + findings.
- `context_panel.py`'s `PanelHooks` (a `RunHooks` subclass) records every agent's context. **Plan:** subclass it with `on_tool_end` to append `{agent, name, args, result}` to the run's `tool_calls` list; that's how `/chat` gets sub-agent calls.
- The starter streams over `GET /run` (SSE) and serves `/context` for the context panel. Ours is `POST /run` (section 7), so it can carry the session; `/chat` is what graders use.
- No clarification gate, Dockerfile, or deploy config in either starter. We add them.
