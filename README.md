# award-flight-researcher

Pair-centric award availability watcher built around the seats.aero Pro API.

Tracks `(outbound, return)` itinerary candidates that satisfy trip-level
constraints — date window, trip length, pax count, cabin, layovers — and
alerts only when a *bookable pair* exists, not when an isolated leg appears.

## Architecture

```
seats.aero ──▶ poller ──▶ legs (sqlite)
                              │
                              ▼
                       pair joiner ──▶ pairs (sqlite)
                              │
                              ▼
                    state-diff alerter ──▶ ntfy / Pushover
```

Polling cadence is per-route, not global: each route+cabin combination is
re-polled on its own interval so hot routes get more attention. Pairs move
through `candidate → viable → alerted → invalidated`. Only `viable`
transitions page you.

## Key invariants

- A pair is **viable** iff both legs have `seats ≥ pax`, nights ∈
  `[nights_min, nights_max]`, both dates ∈ window, layover/stop limits met,
  and total miles fit a funded pool (or 2-ticket split is allowed).
- A leg that disappears invalidates every pair that referenced it.
- Alerts are emitted on transitions, never on steady state — no duplicate
  pages for a pair that's been viable for hours.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/search.example.yaml config/search.yaml
cp config/balances.example.yaml config/balances.yaml
cp .env.example .env   # then fill in SEATSAERO_API_KEY + NTFY_TOPIC or Pushover keys
```

Edit `config/search.yaml` and `config/balances.yaml` for your trip.

## Run

One-shot (poll → join → diff → alert, then exit):

```bash
python -m researcher.cli run-once
```

Long-running loop (poll every N minutes):

```bash
python -m researcher.cli watch --interval-minutes 15
```

Inspect current viable pairs:

```bash
python -m researcher.cli pairs --state viable
```

## State persistence

SQLite file at `data/researcher.db` (configurable). Safe to back up; deleting
it resets all known availability and re-alerts everything that's currently
viable.
