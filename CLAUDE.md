# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dependency management is via `uv`; never invoke `pip` or `python` directly — always go through `uv run`.

```bash
uv sync                                              # install deps from uv.lock into .venv
uv run researcher init-db                            # create sqlite schema (idempotent; runs ALTER migrations)
uv run researcher run-once                           # one poll → enrich → synth → join → alert cycle
uv run researcher watch --interval-minutes 15        # long-running loop
uv run researcher pairs --state viable --limit 50    # inspect pairs (candidate|viable|alerted|invalidated)
uv run pytest                                        # full test suite
uv run pytest tests/test_pairs.py::test_alert_batch_dedups_per_outbound_and_pareto_filters   # single test
```

The CLI entry point is the `researcher` script declared in `pyproject.toml` (`researcher.cli:main`). Every subcommand accepts `--search PATH` and `--balances PATH` to override the default `config/search.yaml` / `config/balances.yaml`.

## Configuration

Three runtime inputs, all gitignored (`config/*.yaml` with `!config/*.example.yaml` override) — copy from the `.example` files:

- `config/search.yaml` — `trip.passengers` + `trip.windows` (list; each window has its own `start`/`end`/`nights.{min,max}`), `routing` (`origins`, `destinations`, `allow_open_jaw`, `max_stops`, `layover_minutes.{min,max}`), `cabins`, seats.aero `sources` to poll, `leg_filters` (`min_seats`, `exclude_overnight_layovers`, `direct_only`). `leg_filters.direct_only` (default `false`) drops non-direct per-cabin offers at ingestion using the seats.aero per-cabin `Direct` flag — no `/trips` enrichment required. The `poll:` block (`cached_interval_minutes`, `live_interval_minutes`, `hot_window_days`, `hot_interval_minutes`) is required by `load_search` (KeyError if missing) but its values are not consumed downstream — it's vestigial config that must still be present. `routing.max_stops` and `leg_filters.exclude_overnight_layovers` are parsed but currently unused.
- `config/balances.yaml` — `direct:` airline-program balances, `transferable:` pools with their `targets`, and a `limits:` block carrying `max_fees_per_pax_usd` and `max_miles_per_pax[cabin]` (both optional; default to effectively ∞).
- `.env` — `SEATSAERO_API_KEY` is required; pick `NTFY_TOPIC` (+ optional `NTFY_SERVER`, default `https://ntfy.sh`) and/or `PUSHOVER_TOKEN`+`PUSHOVER_USER` for alerting; optional `RESEARCHER_DB_PATH` (default `data/researcher.db`), `RESEARCHER_LOG_LEVEL` (default `INFO`).

`load_env()` raises if `SEATSAERO_API_KEY` is missing — all CLI commands except `--help` will fail without it.

## Architecture

The system is **pair-centric**: it only alerts when an `(outbound, return)` itinerary pair satisfies every trip constraint *together*. One full cycle:

```
seats.aero ─▶ poller (cli._poll_once) ─▶ legs table
   │            (per window × route × direction)
   │
   ├──▶ /trips/{availability_id} enrichment (pairs.enrich_layovers)
   │        populates legs.segments_json, duration_min, stops, carriers
   │        computes legs.meets_layover_filter from current config
   │
   ├──▶ pax-split synthesis (pairs.synthesize_pax_splits)
   │        upserts cabin='mixed' legs for flights where no single cabin
   │        has ≥ pax seats but combined cabins do
   │
   ├──▶ pair join (pairs.join_pairs, SQL self-join over legs)
   │        respects per-window date+nights bounds, mixed cabins allowed
   │
   └──▶ alert batch (alerts.build_alerts_for_new_viable)
            per-outbound dedup, then Pareto frontier on (miles, duration)
            suppressed pairs marked 'alerted' alongside winners
            dispatch via ntfy / Pushover
```

### Module map

- `src/researcher/cli.py` — Click entry points. `_poll_once` is the orchestrator: iterate `(window × directional route)`, call `cached_search`, upsert legs, then run `enrich_layovers` → `synthesize_pax_splits` → `join_pairs` → `build_alerts_for_new_viable` → `dispatch` + `mark_alerted` (on the full `batch.all_pair_ids`).
- `src/researcher/seatsaero.py` — HTTP client (`/search`, `/trips/{id}`), `normalize()` flattening per-cabin search fields into `LegRow`s with `availability_id` (and honoring `direct_only` at flatten time), layover/quality helpers (`trip_layovers_minutes`, `any_trip_within_layover_window`, `best_trip_quality`), URL builders (`availability_url` → `/i/{id}` deep link, `search_url` → fallback for synthetic legs without an ID), and the `RateLimited` exception raised on any 429 from `cached_search` / `trip`.
- `src/researcher/pairs.py` — `upsert_leg` (insert/update; `availability_id` is `COALESCE`-protected on update so a stale ID is never overwritten), `join_pairs` (the core SQL join + state transitions), `enrich_layovers` (one /trips fetch per leg lacking `segments_json`, skipping synthetic `cabin='mixed'` legs, with config-aware filter recomputation every cycle, propagating `RateLimited` to abort the cycle), `synthesize_pax_splits` (writes synthetic `cabin='mixed'` legs).
- `src/researcher/pricing.py` — Cap vs balance checks are split: `_check_caps` is per-leg and per-cabin; `_check_balance` is program-level. `pair_feasibility` calls caps once per leg (supporting different cabins) and then aggregates balance across the pair when both legs share a program. `best_pax_split` enumerates valid `pax_count_per_cabin` splits for one flight's offers and returns the cheapest that passes caps. `assess` is a single-program leg-level helper retained but currently unused outside the module.
- `src/researcher/alerts.py` — `build_alerts_for_new_viable` returns `AlertBatch(to_send, suppressed_pair_ids)` after per-outbound-flight dedup + Pareto frontier. The caller must `mark_alerted` on `batch.all_pair_ids` (both lists) so suppressed pairs don't re-queue. Body rendering expands per-cabin pax-split breakdown from `legs.last_snapshot_json` when cabin is `mixed`. `dispatch` chooses ntfy and/or Pushover based on which env vars are set.
- `src/researcher/db.py` — sqlite schema + `connect` (`foreign_keys=ON`, WAL, autocommit) + `transaction()` (explicit BEGIN/COMMIT/ROLLBACK). `init()` runs idempotent ALTER migrations via `_migrate_add_columns` for columns added after the initial schema (`availability_id`, `segments_json`, `meets_layover_filter`, `carriers`). The schema also defines a `poll_log` table that `_poll_once` writes one row per `(origin, destination, window)` poll on success or one summary row on `RateLimited`.
- `src/researcher/config.py` — frozen dataclasses + YAML/env loaders. `DateWindow` carries its own `nights_min`/`nights_max`. `BalancesConfig.effective_balance(program)` is the canonical "direct + every transferable pool targeting this program" calc.

### State machine for pairs

```
candidate ──(feasible)──▶ viable ──(alert dispatched)──▶ alerted
    │                       │                                │
    │                       │                                ▲
    │                       │                  (re-seen, still feasible:
    │                       │                   alerted stays alerted)
    │                       │                                │
    └───────────────────────┴────(no longer in join)────────▶ invalidated
```

- `candidate`: pair satisfies the SQL join but `pair_feasibility` says no funded pool covers it.
- `viable`: feasible but not yet alerted. **Only `candidate → viable` and new-as-viable transitions enqueue alerts.** Suppression by dedup/Pareto then routes some `viable` pairs straight to `alerted` without a notification.
- `alerted`: a notification went out (or was suppressed). **Sticky as long as feasibility holds** — see the `effective_state` branch in `join_pairs` that preserves `alerted` over a recomputed `viable`. Without this, every cycle would re-page every viable pair.
- `invalidated`: terminal-until-rejoined. If the underlying legs reappear and satisfy the join, the pair record (keyed on `out_leg_id, ret_leg_id`) is reused.

Every transition is appended to `pair_events` (`old_state, new_state, ts, note`) for auditability.

### Important invariants

- **Leg uniqueness** is `(source, origin, destination, depart_date, cabin)`. Re-polls update seats/miles/fees in place; the row id is stable.
- **Pair uniqueness** is `(out_leg_id, ret_leg_id)`. The pair survives a re-poll as long as both legs survive.
- **Foreign keys are ON with `ON DELETE CASCADE`** — deleting a leg deletes every pair referencing it. The current code never deletes legs.
- **Nights math is done in SQL** via `CAST(julianday(r.depart_date) - julianday(o.depart_date) AS INTEGER)`. Don't move this to Python without preserving the int truncation semantics.
- **Same-window pair rule**: the join SQL OR's a window-scoped predicate per `DateWindow`. Both legs of a pair must satisfy the *same* window (date range + nights bounds), so different-length trips coexist under one config without spurious cross-window pairs.
- **Mixed cabins** are allowed across legs — the join does NOT enforce `o.cabin = r.cabin`. `pair_feasibility` takes `out_cabin` and `ret_cabin` separately and applies each cap per leg.
- **`cabin='mixed'` is a synthetic leg** generated by `synthesize_pax_splits` for flights needing intra-leg pax-split. It carries averaged per-pax miles + a JSON breakdown in `last_snapshot_json`. Per-cabin caps are checked at split-construction time, not at pair-feasibility time; the pair-level `_check_caps` skips when `cabin` isn't in `max_miles_per_pax`.
- **Layover filter participates in the join**: `meets_layover_filter` is recomputed every cycle from `segments_json` against the *current* config, and the join uses `(IS NULL OR = 1)` so un-enriched legs (and synthetic mixed legs) pass through until enriched.
- **Open-jaw filtering** happens in Python after the SQL join. The SQL itself always permits open-jaw because origins/destinations are matched against the full sets.
- **Alert suppression must mark suppressed pairs alerted** — `cli._poll_once` calls `mark_alerted(batch.all_pair_ids)`, not just winners, or the dedup loop would re-process them forever.
- **seats.aero Pro quota is 1000 calls/calendar day** (resets midnight UTC), tracked in the `X-RateLimit-Remaining` response header. Each cycle burns ~72 search calls (windows × routes × directions) plus one `/trips` call per newly-discovered leg, so cron cadence must keep total daily calls under 1000 across all matrix entries sharing the API key. A 429 on any call raises `seatsaero.RateLimited`, which aborts the whole cycle (every remaining call would also 429); `run-once` exits non-zero so the CI job fails loudly instead of silently masking the lockout.
- **`availability_id` is sticky on a leg** — `upsert_leg` `COALESCE`s it on UPDATE, so once a row has been populated with an ID, subsequent re-polls cannot replace it. Synthetic `cabin='mixed'` legs never have an `availability_id` and `enrich_layovers` skips them.

### Where to add functionality

- **New alert sink** → add `_send_*` in `alerts.py`, branch in `dispatch`, add env field in `config.Env`.
- **New seats.aero cabin or field** → extend `CABIN_FIELD` in `seatsaero.py`.
- **New feasibility rule at pair level** → extend `pair_feasibility` in `pricing.py`; the `Feasibility.reason` string flows into `pair_events.note` for debugging.
- **New per-leg quality field from /trips** → add a column in `db.SCHEMA` and `_migrate_add_columns`, populate in `enrich_layovers`, surface in the alert query / `_describe_leg` if user-visible.
- **New alert noise filter** → add another reduction step in `build_alerts_for_new_viable` between the existing dedup and Pareto passes (or after Pareto). Make sure rejected pair_ids end up in `suppressed_pair_ids`.
- **New leg-level filter from config** → either tighten the SQL in `join_pairs` (preferred when the data is on `legs`) or filter in `seatsaero.normalize` / `cli._poll_once` (when the source data is pre-DB).

## Testing

Tests live in `tests/`:

- `tests/test_pairs.py` runs against a real sqlite database in `tmp_path`. Pattern: build a minimal `SearchConfig` + `BalancesConfig` via helpers (`_search`, `_balances`, `_leg`), `upsert_leg` directly, call `join_pairs` (and optionally `synthesize_pax_splits` / `build_alerts_for_new_viable`), assert on `JoinStats` / `AlertBatch` / direct row queries. Add tests in this style — don't mock the database.
- `tests/test_seatsaero.py` exercises the HTTP client via `httpx.MockTransport`: covers `normalize(direct_only=...)` filtering, the happy path, and the 429 → `RateLimited` propagation on both `cached_search` and `trip`.

## Deployment

Two shapes:

1. **`researcher watch` long-running** on a host with persistent disk for the sqlite DB.
2. **Scheduled CI** via `.gitea/workflows/poll.yml`: cron `0 */2 * * *` (every 2 hours ≈ 12 cycles/day per region, sized to stay under the seats.aero 1000-calls/day cap with headroom for `/trips` enrichment). Runs one matrix job per region. Matrix entries come from a `setup` job that passes through the `POLL_MATRIX` Actions variable (so the region list isn't committed). Each entry is `{ name, search, db }`; the job uses the MinIO `mc` client (aliased as `homelab` against `S3_ENDPOINT`) to pull `<search>` (renamed to `config/search.yaml` for the run) + the shared `balances.yaml` + `<db>` from MinIO, runs `init-db` + `run-once` with `RESEARCHER_DB_PATH=data/<db>`, then pushes the DB back. Concurrency is scoped per region (`poll-<name>`), so regions run in parallel without racing each other's DB. Required Actions variables: `S3_ENDPOINT`, `S3_BUCKET`, `S3_PREFIX` (defaults to `award-flight-researcher`), `POLL_MATRIX` (JSON array). Required secrets: `SEATSAERO_API_KEY`, `PUSHOVER_TOKEN`, `PUSHOVER_USER`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`. The CI path wires Pushover only — `NTFY_*` env vars are not passed through, so deployed alerts go via Pushover even though the code supports both. Each region has its own DB and alert stream — `balances.yaml` is shared, so multi-region setups assume balance pools don't need to be partitioned across regions.
