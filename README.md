# NomadReady

**Find where to work remotely for a week or more, and which neighbourhood to stay in.**

Live: **https://nomadready-wuuyn7pbbq-uc.a.run.app** · Path: Deep Research · Author: ch3991

NomadReady is a deep-research agent for remote workers planning a stay abroad. Tell it when you're going, for how long, your budget and the hours you need to be online. A Lead agent sends one Researcher per city in parallel. They query real Airbnb availability for your exact dates, measured home-internet speeds around each listing, the weather for your month, your work hours in local time and the U.S. travel advisory, and they search Google Maps for coworking. A Writer turns the findings into a cited report, and a Verifier checks every number against the raw tool results.

## Who it's for

Remote workers, freelancers and students on breaks who want to spend 1 week to 6 months working from another city. They have hard constraints that generic "best cities for digital nomads" lists ignore:

- **A budget** for *these* dates, not an average rent.
- **Online hours** that must fit a home time zone (9–5 Eastern is 14:00–22:00 in Lisbon).
- **Internet** they can rely on.

NomadReady answers "which of these cities work for me, and where in each should I stay?" with numbers, and says plainly what its data can't tell you.

## How to use it

Open the page and either fill in **Trip details** (cities, month, length, budget, online hours and time zone, workspace, coworking) or just type a question. You can also click one of the examples. A rough time and length are enough: "a few weeks this summer" works. While it runs, you can watch each agent work: a timeline and one lane per agent, with every tool call and its result. The answer includes:

- **Your search** as chips (dates, budget and room type, online hours, cities), plus anything the agent assumed.
- **A ranking:** one card per city, best first, with its fit, a one-line verdict, median stay cost, download speed and your work hours locally.
- **A side-by-side comparison** of any two cities (the top two to start; click "Add to compare" on another). A **Show** menu narrows it to one feature. Cards: cost of the stay, home internet, work hours on a 24-hour bar, typical weather (°F, with °C), daylight (sunrise, sunset, and how much of your workday is in daylight), the U.S. travel advisory, and where to stay. "Where to stay" is a neighbourhood table with two example listings each: an affordable one (cheapest third, same room type) and a typical one (nearest that neighbourhood's median). Where the difference is clear, a "Better" badge marks the stronger city on cost, internet, work hours and advisory.
- **Panels** for the full written report, how the agents got there, the numbered sources, and "What this data can't tell you". Every number carries a citation chip that opens its source.

If the question is too vague ("somewhere warm and cheap?"), it asks for what's missing first.

**Follow-ups in the same chat.** To change the trip, just say what to change: "make it $2,000", "add Porto", "drop Lisbon", "what if I go in May?". NomadReady keeps everything else and researches again. To ask about the answer you already have ("which of those has the fastest internet?", "why is Lisbon last?", "is Palermo safe?"), it answers in about 10–15 seconds from that report alone, with the same citations, and no new research. A code check makes sure every number in that quick answer appears in the report. If the report doesn't cover the question (a neighbourhood it didn't list, say), the full research runs instead. "Thanks" gets a one-line reply, and **New search** starts over.

**API.** `POST /chat` keeps the Project 1 shape. Every tool call, including the sub-agents', is in `tool_calls` with an `agent` field:

```bash
curl -X POST https://nomadready-wuuyn7pbbq-uc.a.run.app/chat -H 'content-type: application/json' \
  -d '{"message": "Lisbon or Porto for 3 weeks in April, about $1,800 total, online 9-5 Eastern", "session_id": null}'
```
```json
{"response": "**Bottom line**: ... [airbnb-porto] ... ## Sources ...",
 "session_id": "0bac843b-...",
 "tool_calls": [{"agent": "Lead", "name": "research_city", "args": {"city": "Porto", "task": "..."}, "result": "..."},
                {"agent": "Researcher-Porto", "name": "find_stays", "args": {"city": "Porto", "check_in": "2027-04-01", "...": "..."}, "result": "..."}]}
```

Send the returned `session_id` back to continue the conversation (e.g. to answer a clarifying question). `POST /run` takes the same body and streams progress as server-sent events (the page uses it). `GET /about` lists the covered cities and data dates.

## How it works

```
question ─► Lead (intake) ─── vague? ─► clarifying question
                │
                ▼ trip brief (dates, nights, budget in USD, work hours, room type, ...)
            Lead (research) ── research_city × N in ONE turn ──► Researcher-Lisbon ┐ parallel, each in a
                │                                               ► Researcher-Porto  ┘ fresh context with
                │ ◄─────────────── structured, cited findings ──────────────────────┘ the data tools
                │ gaps? (<5 stays in budget, low-confidence speeds, bad overlap, coworking, a failure)
                ├──► follow-up Researcher ("Researcher-Porto #2"), up to 2 more rounds
                ▼
            Writer ──► Verifier (checks every number against raw tool results) ──► one revision ──► report + Sources
```

- **Four agent shapes, different jobs:**
  - **Lead:** intake and clarification, then planning, fan-out and gap-filling.
  - **City Researcher:** one city, with the data tools.
  - **Writer:** the report, written only from the findings.
  - **Verifier:** fact-checks the draft.
  
  All of them run on Gemini 3.5 Flash on Vertex AI, through the OpenAI Agents SDK and LiteLLM.
- **Parallel fan-out:** the Lead calls `research_city` once per city in a single turn. The SDK runs those calls concurrently, and each Researcher starts with a fresh context.
- **Gap loop:** after the first round, the Lead checks the findings against explicit gap rules and sends targeted follow-ups. It's capped at 3 rounds, 10 Researchers, and no new Researchers after 120 s.
- **Clarification:** code, not the model, decides. Without a named city it needs a rough time, a length, a budget and work hours (or "flexible"). With a city, it still needs a rough time and length, because prices, availability and weather depend on them; a missing budget or missing hours are stated as assumptions instead.
- **Citations:** every tool result carries citation ids (`airbnb-lisbon`, `ookla-fixed-2026-Q3`, `open-meteo-lisbon-04`, `state-dept-pt`, `fx-EUR`, `iana-tz`, `google-places-…`). The Writer cites them inline. The **Sources list is built in code** from what the tools actually returned, and any id that isn't a real tool citation is marked `[unverified]`.
- **Accuracy checks:**
  - Code fixes neighbourhood numbers a Researcher garbled while copying them.
  - Code flags any number in the draft that no tool returned, and the Verifier judges those (derived correctly, wrong, or unsupported). The Writer then revises once.
  - The headline numbers on each city card are read straight from the tool results.
- **Reliability:** each Researcher is capped at 100 s, the research phase at 180 s, and the Writer has a deadline, so a stalled model call degrades the answer instead of hanging it. Calls that Vertex rate-limits on the `global` endpoint are retried on the `us` endpoint.

## Data

Covers **20 cities**: Lisbon, Porto, Barcelona, Madrid, Rome, Athens, Berlin, Amsterdam, Prague, Istanbul, Cape Town, Mexico City, Buenos Aires, Rio de Janeiro, Santiago, Montreal, Vancouver, Bangkok, Tokyo, Taipei.

| Source | What we use | How it's loaded |
|---|---|---|
| **[Inside Airbnb](https://insideairbnb.com/get-the-data/)**: `listings.csv.gz` + `calendar.csv.gz` per city, Sept 2026 snapshots (Sep 14–28) | 385K listings with price, location, room type, bedrooms, workspace/Wi-Fi amenities, Airbnb's own Wi-Fi speed test where shown; the daily calendar (~160M rows) compressed into 950K "open stretches" of 7+ nights | Offline into DuckDB (`data/nomad.duckdb`) |
| **[Ookla Speedtest open data](https://github.com/teamookla/ookla-open-data)**: fixed + mobile performance tiles, 2026 Q3 | 126K zoom-16 tiles (~600 m) around the cities: download/upload Mbps, latency, number of tests | Offline, filtered to each city's area; **joined to every listing by computing its quadkey**, so each listing has the measured speed of the block it's on (97–100% coverage) |
| **[Open-Meteo](https://open-meteo.com)** historical weather (ERA5), 2021–2025 | Monthly averages of daily high/low, rainy days, sunshine, daylight at each city's center | Fetched once into a `climate` table (240 rows) |
| **[U.S. State Department travel advisories](https://cadataapi.state.gov/api/TravelAdvisories)** | Country advisory level and link | Live API (cached 1 h) |
| **Exchange rates**: [Frankfurter](https://www.frankfurter.app) (ECB), [ExchangeRate-API](https://www.exchangerate-api.com) for ARS/CLP/TWD | USD per unit of each local currency | Fetched once at build time into `fx_rates` |
| **[Google Maps Places API](https://developers.google.com/maps/documentation/places/web-service)** | Coworking spaces within ~1.5 km of a neighbourhood: count, ratings, hours, website | Live at answer time; **not stored**, per Google's terms |

**The interesting part:** no model knows how many Airbnb stays are actually free for *your* dates under *your* budget, or the measured fixed-internet speed in the blocks where those listings are. The listing ⨝ speed-tile join gives neighbourhood-level internet speed for listings people can actually book. A stay "fits" when an open stretch covers every night, the host's minimum and maximum stay allow it, and the snapshot nightly price × nights (in USD) is within budget.

**How to load it.** The data isn't in this repo (Inside Airbnb asks that its data not be republished). To rebuild it:

```bash
uv sync
uv run scripts/download_airbnb.py      # ~600 MB of listings + calendars (cities in scripts/cities.py)
uv run scripts/download_ookla.py       # latest Ookla quarter, filtered to the cities' bounding boxes
uv run scripts/build_db.py             # -> data/nomad.duckdb (~150 MB, ~25 s), with self-checks
uv run scripts/build_climate.py        # monthly climate averages (first build only; cached)
```

The deployed app gets the same database from a private GCS bucket, copied into the container image at build time.

**What the data can't answer, and the agent says so:**
- Visa rules and how long you can legally stay.
- Neighbourhood safety, noise or "vibe".
- Live prices, or weekly and monthly discounts: prices are the September snapshot's nightly rate × nights, so long stays are probably overestimated.
- Whether specific dates were booked after the snapshot.
- A particular apartment's Wi-Fi: Ookla measures the surrounding block, though about 6% of listings show Airbnb's own speed test.
- Coworking prices or community.
- Cities and areas outside the 20 covered.
- Hotels and leases.

In Mexico City, Inside Airbnb's neighbourhoods are boroughs (Cuauhtémoc, Benito Juárez), not colonias like Roma or Condesa.

## Sample queries

Each sample was run through NomadReady and through **the same model (Gemini 3.5 Flash) with no tools and no system prompt**. The full outputs, and the expected values computed directly with the data tools, are in [`docs/samples/`](docs/samples/), regenerated by `uv run scripts/run_samples.py`. Coworking names and ratings are removed from the saved outputs because Google's terms don't allow storing them.

### 1. Compare three cities

> A month in February, $2,500/month for a 1BR with a dedicated workspace, online 9am–5pm Eastern. Compare Lisbon, Mexico City, and Buenos Aires.

**Expected answer.** These are whole 1BR places with a dedicated workspace, open Feb 1 – Mar 1, 2027 (28 nights), with a $2,500 budget:

| | Buenos Aires | Mexico City | Lisbon: city proper · whole metro area |
|---|---|---|---|
| Stays open / within $2,500 | 7,439 / **4,779** | 7,251 / **3,328** | 2,361 / **46** · 3,204 / **113** |
| Median stay total (10th pct) | $2,124 ($1,298) | $2,635 ($1,454) | $5,212 ($3,344) · $4,998 ($3,111) |
| Best-supplied neighbourhoods (in budget, median, fixed down) | Palermo 1,398 · $1,882 · 271 Mbps | Cuauhtémoc 1,493 · $2,007 · 196 Mbps | Parque das Nações 10 · $2,057 · 406 Mbps |
| 9–5 Eastern in local time | 11:00–19:00 | 08:00–16:00 | 14:00–22:00 |
| February high / low | 82 / 68 °F (27.9 / 20.1 °C) | 76 / 45 °F (24.5 / 7.2 °C) | 62 / 50 °F (16.6 / 9.8 °C) |
| U.S. advisory | Level 1 | Level 2 | Level 1 |

Sources: `listings` + `availability` (Inside Airbnb), `speed_tiles` (Ookla) joined on quadkey, `fx_rates`, `climate` (Open-Meteo), State Dept advisories API, IANA time zones. Lisbon's data covers the whole metro area, including Cascais, Sintra and other towns; either scope is a correct answer as long as the report says which one it used.

| NomadReady (166 s: Lead + 3 parallel Researchers + a Lisbon follow-up + Writer, 30 tool calls) | Same model, no tools |
|---|---|
| Ranks **Buenos Aires** first (4,779 in budget, median $2,124), then Mexico City (3,328, median $2,635), then **Lisbon as a tight fit: "only 113 options under budget in the metropolitan area"**, median $4,998 (10th percentile $3,111). These match the expected values; this run used Lisbon's whole metro area. The Buenos Aires and Mexico City neighbourhood tables match exactly (Palermo 1,398 · $1,882 · 271 Mbps; Cuauhtémoc 1,493 · $2,007 · 196 Mbps). For Lisbon, a follow-up Researcher picked central neighbourhoods with only 3–5 stays in budget each (Estrela, Campo de Ourique, Arroios), not Parque das Nações. Each neighbourhood gets an affordable and a typical example listing (Palermo: $827 and $1,883). Temperatures in °F (Buenos Aires highs around 82°F). | Ranks **Mexico City** first: "A stylish 1BR in prime, safe neighborhoods… will cost **$1,200 – $1,600**" (the data's median for the month is $2,635). For Lisbon: "a nice 1BR with a dedicated workspace in central areas… for around **$1,500 – $1,800**" (in the data, 113 of 3,204 open 1BRs with a workspace fit under $2,500). Buenos Aires 1BRs with a pool for "$800 – $1,200" (the median is $2,124). No measured internet speeds: "ask for a screenshot of a speed test". |

### 2. Neighbourhood drill-down

> In Mexico City, which neighborhoods have month-long rentals in March under $1,500 and the fastest home internet?

**Expected answer** (Mar 1–29, 2027, under $1,500; neighbourhoods with 20+ stays in budget, ranked by the median measured fixed download speed of their listings' blocks). Citywide: 5,528 of 17,739 open stays fit; median $2,087.

| Neighbourhood | Stays under $1,500 | Median stay total | Median fixed download |
|---|---|---|---|
| Benito Juárez | 802 | $1,061 | **219 Mbps** |
| Miguel Hidalgo | 499 | $1,096 | 214 Mbps |
| Cuajimalpa de Morelos | 40 | $1,139 | 209 Mbps |
| La Magdalena Contreras | 38 | $1,204 | 209 Mbps |
| Cuauhtémoc | 1,735 | $1,061 | 201 Mbps |

Sources: `listings` + `availability` ⨝ `speed_tiles` on quadkey, grouped by `neighbourhood` (Inside Airbnb's neighbourhoods here are boroughs).

| NomadReady (89 s, 5 tool calls) | Same model, no tools |
|---|---|
| A table "ordered by median fixed download speed, with the fastest first": **Benito Juárez 802 · $1,061 · 219 Mbps; Miguel Hidalgo 499 · $1,096 · 214 Mbps; Cuajimalpa de Morelos 40 · $1,139 · 209 Mbps**, all high-confidence speeds. It then names the other two: Cuauhtémoc (the most stays, 1,735, at 201 Mbps) and La Magdalena Contreras (38 stays, 209 Mbps). All five match the expected values. Each pick has an affordable and a typical private room (Benito Juárez: $498 and $1,061). It also gives the citywide 5,528 of 17,739 in budget, and says work hours weren't checked because none were given. Because the question asks for the *fastest* internet, the brief records `rank_neighbourhoods_by: internet` and the Researcher ranks by speed, not by number of stays. Three test runs all picked the same three. | Reads the budget as "**$1,500 USD per month**" and names colonias (Narvarte, Roma Sur, Escandón…): fiber "between **200 Mbps and 1 Gbps**", "speeds of 300+ Mbps", 1–2BRs for "$1,000–$1,300 USD". Plausible local knowledge, but every number is unsourced and none comes from availability for March. |

### 3. Partially answerable

> Can I stay in Lisbon for 6 months starting in January, and is the coworking scene good?

**Expected answer.** Housing: 3,640 listings in Lisbon proper are open for the whole 182 nights from Jan 1, 2027 (5,078 in the metro area). The median 6-month total is $28,012 and the cheapest 10% are under $13,264, at snapshot nightly rates, so monthly discounts would lower these. The central options are Santa Maria Maior (875 stays, median $32,655, 268 Mbps) and Arroios (480, $23,267, 286 Mbps). January averages 59 / 48 °F (15.2 / 9.0 °C). **Coworking:** live Google Maps results near those neighbourhoods (17–20 spaces each at the time of the run). **Visas are not in any of the data**, so "can I legally stay 6 months?" must be declined, not answered.

Sources: `listings` + `availability`, `speed_tiles`, `climate`, Google Places API (live); visas: none.

| NomadReady (68 s, 7 tool calls) | Same model, no tools |
|---|---|
| "3,640 available listings", median $28,012, 10th percentile $13,264. Arroios 480 · $23,267 · 286 Mbps, Santa Maria Maior 875 · $32,655 · 268 Mbps and Misericórdia 558 · $33,030 · 256 Mbps, with 19, 17 and 18 coworking spaces nearby from Google Maps. These match the expected values. Under "What this data can't tell you": **"Local visa requirements… cannot be definitively evaluated from the database facts"**, along with coworking prices and desk availability. January highs 59°F, lows 48°F. It assumed no budget and no work hours, and says so at the top. | "The short answer is **yes, absolutely**", then visa advice from memory (90/180 rule, D8 digital-nomad visa) with no source or date. Coworking is "one of the best… in the world". No availability or price for these dates. |

### Vague question → clarification

> Where should I go somewhere warm and cheap?

NomadReady (3 s, no tools): *"To compare cities for you I need a bit more: roughly when you're going (dates, a month, or a season); roughly how long you'll stay (e.g. 2 weeks, a month, 3 months); a budget for the stay in US dollars; the hours you need to be online and your home time zone (or say they're flexible)…"*

Follow-up in the same session: *"A month starting in February, about $1,800 total, online 9-5 Eastern, and I need a desk."* No cities were named, so the Lead picked four warm candidates and researched them in parallel: **Santiago, Buenos Aires, Rio de Janeiro and Mexico City** (136 s, 23 tool calls). Santiago ranked first: 4,032 of 6,702 open stays within budget, median $1,590 for 28 nights, and the central Santiago commune alone has 2,185 stays within budget on blocks measuring 476 Mbps. Mexico City came last because "its February nights average a chilly 45°F".

## Running locally

```bash
uv sync
gcloud auth application-default login       # Vertex AI (Gemini) credentials
echo "GOOGLE_PLACES_API_KEY=..." > .env     # optional: without it, coworking is reported as unknown
# build data/nomad.duckdb (see "How to load it"), then:
uv run app.py                               # http://127.0.0.1:8000
uv run pytest -m "not network"              # 91 offline tests
```

## Deployment

Cloud Run (`us-central1`) with continuous deploy: a Cloud Build trigger on every push to `main` runs [`cloudbuild.yaml`](cloudbuild.yaml). That copies the database from a private GCS bucket, builds the [`Dockerfile`](Dockerfile), pushes the image to Artifact Registry and deploys. Settings: 600 s request timeout, scale to zero, one instance (sessions are in memory), and the Places key from Secret Manager.

## Repo layout

```
app.py              FastAPI: /chat, /run (SSE), /about, /clear, / (the page)
scout/              agents: intake.py (Lead phase 1), lead.py (fan-out, gap loop), researcher.py,
                    writer.py, verify.py, pipeline.py, summary.py (city-card numbers), config.py (models, caps)
tools/              data tools: stays.py (find_stays, neighbourhood_speed), weather.py, advisory.py,
                    fx.py, overlap.py, coworking.py, db.py (read-only DuckDB, SQL tool)
static/index.html   the page
scripts/            offline data pipeline + run_samples.py
docs/samples/       sample outputs (expected, agent, baseline)
tests/              offline tests (+ network tests)
```

## Credits

- Listing and calendar data: [Inside Airbnb](https://insideairbnb.com), used under its terms; not redistributed here.
- Speedtest® by Ookla® Global Fixed and Mobile Network Performance Maps, based on analysis by Ookla of Speedtest Intelligence® data for 2026 Q3. Provided by Ookla and accessed November 2026. Ookla trademarks used under license and reprinted with permission. Data licensed [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
- Weather data by [Open-Meteo.com](https://open-meteo.com) (ERA5 reanalysis), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Travel advisories: U.S. Department of State.
- Exchange rates: [Frankfurter](https://www.frankfurter.app) (European Central Bank reference rates) and [Rates By Exchange Rate API](https://www.exchangerate-api.com).
- Coworking results: Google Maps (Places API), shown live with attribution and never stored.
