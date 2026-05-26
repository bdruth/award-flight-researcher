# award-flight-researcher

Pair-centric award availability watcher built around the seats.aero Pro API.

Tracks `(outbound, return)` itinerary candidates that satisfy trip-level
constraints — date windows, trip length, pax count, cabins, layovers — and
alerts only when a *bookable pair* exists, never on an isolated leg.

## Architecture

```
seats.aero ─▶ poller ─▶ legs (sqlite)
                          │
                          ▼  (per-leg /trips enrichment: layovers,
                          │   duration, stops, carriers)
                          ▼
                   pax-split synthesizer ─▶ adds synthetic 'mixed' legs
                          │                  for flights needing a cross-cabin split
                          ▼
                   pair joiner ─▶ pairs (sqlite)
                          │
                          ▼
            alert builder ─▶ per-outbound dedup + Pareto frontier
                          │
                          ▼
                   ntfy / Pushover
```

Each cycle: poll the configured date windows + routes, enrich any new legs via
`/trips/{id}`, synthesize mixed-cabin legs when no single cabin satisfies pax,
join into pairs, then dispatch alerts for newly-viable pairs that survive
dedup + Pareto filtering.

## Key behaviors

- A pair is **viable** iff both legs have `seats ≥ pax`, both depart dates fall
  in the **same** configured window with `nights` within that window's bounds,
  every layover sits within the configured `[min, max]` minutes, and the total
  miles fit a funded pool (or a two-ticket split).
- **Pairs may mix cabins** across legs (e.g. business out, economy back). Caps
  are checked per leg against each leg's cabin; balance draw aggregates across
  the pair when both legs share a program.
- **Pax may split across cabins on one flight**: when no single cabin has ≥ pax
  seats but combined cabins do, a synthetic `cabin='mixed'` leg is generated
  representing the cheapest valid split. Pairs flow through the same machinery.
- Pairs move through `candidate → viable → alerted → invalidated`. Alerts fire
  only on `candidate → viable` (or new-as-viable) transitions; the `alerted`
  state is sticky as long as feasibility holds, so no re-paging.
- A leg that disappears invalidates every pair that referenced it.
- **Alert noise filter**: per outbound (origin, destination, depart_date), keep
  only the cheapest+shortest pair; then drop anything dominated on
  `(total_miles, total_duration)` by another surviving pair. Suppressed pairs
  are silently marked `alerted` so they don't re-queue.
- Alerts include per-leg duration / stops / carriers and a
  `https://seats.aero/i/{availability_id}` deep link.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                 # creates .venv, installs deps from uv.lock
cp config/search.example.yaml config/search.yaml
cp config/balances.example.yaml config/balances.yaml
cp .env.example .env                    # then fill in SEATSAERO_API_KEY + alerting creds
```

Edit `config/search.yaml` and `config/balances.yaml` for your trip. The
configs (anything matching `config/*.yaml` except `*.example.yaml`) are
gitignored.

### Multi-window trips

`trip.windows` is a list; each window carries its own `nights`. A pair must
have both legs in the **same** window — useful when planning trips of
different lengths under one config:

```yaml
trip:
  passengers: 2
  windows:
    - { start: 2099-06-01, end: 2099-07-01, nights: { min: 10, max: 14 } }
    - { start: 2099-12-15, end: 2099-12-31, nights: { min: 7, max: 7 } }
```

## Run

```bash
uv run researcher init-db                            # create sqlite schema (idempotent)
uv run researcher run-once                           # one full cycle, then exit
uv run researcher watch --interval-minutes 15        # long-running loop
uv run researcher pairs --state viable               # inspect pairs (candidate|viable|alerted|invalidated)
```

## Tests

```bash
uv run pytest
```

## Deployment

Two reasonable shapes:

1. **Long-running `researcher watch` on a homelab box** — simplest and most
   reliable cadence; the sqlite DB lives next to the process.
2. **Scheduled CI workflow** — `.gitea/workflows/poll.yml` runs `run-once` on
   every cron tick (default `*/15`). State (sqlite DB) plus `search.yaml` and
   `balances.yaml` are pulled from an S3-compatible bucket at job start and the
   DB is pushed back at the end. Requires repo Action secrets:
   `SEATSAERO_API_KEY`, `PUSHOVER_TOKEN`, `PUSHOVER_USER`,
   `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (S3/MinIO).

The workflow targets a self-hosted runner with LAN access to the bucket.

## State persistence

SQLite (default `data/researcher.db`, `RESEARCHER_DB_PATH` overrides). Deleting
the DB resets all known availability — the next cycle treats every viable pair
as new and pages once per surviving alert after dedup/Pareto. Safe to back up;
WAL mode is enabled.
