# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dependency management is via `uv`; never invoke `pip` or `python` directly — always go through `uv run`.

```bash
uv sync                                              # install deps from uv.lock into .venv
uv run researcher init-db                            # create sqlite schema (idempotent)
uv run researcher run-once                           # one poll → join → diff → alert cycle
uv run researcher watch --interval-minutes 15        # long-running loop
uv run researcher pairs --state viable               # inspect pairs (candidate|viable|alerted|invalidated)
uv run pytest                                        # full test suite
uv run pytest tests/test_pairs.py::test_pair_within_night_window_promotes_viable   # single test
```

The CLI entry point is the `researcher` script declared in `pyproject.toml` (`researcher.cli:main`).

## Configuration

Three runtime inputs, all gitignored — copy from the `.example` files:

- `config/search.yaml` — trip window, routing, cabins, seats.aero `sources` to poll, poll cadence, leg filters.
- `config/balances.yaml` — direct airline-program balances + transferable pool balances with their target programs, plus cost ceilings (`max_fees_per_pax_usd`, `max_miles_per_pax[cabin]`).
- `.env` — `SEATSAERO_API_KEY` is required; pick `NTFY_TOPIC` (+ optional `NTFY_SERVER`) and/or `PUSHOVER_TOKEN`+`PUSHOVER_USER` for alerting; optional `RESEARCHER_DB_PATH`, `RESEARCHER_LOG_LEVEL`.

`load_env()` raises if `SEATSAERO_API_KEY` is missing — all CLI commands except `--help` will fail without it.

## Architecture

The system is **pair-centric**: it does not alert on individual leg availability. It alerts only when an `(outbound, return)` itinerary pair satisfies every trip constraint *together*. One full cycle is:

```
seats.aero ─▶ poller (cli._poll_once) ─▶ legs table
                                            │
                                            ▼  (SQL self-join in pairs.join_pairs)
                                         pairs table  ──▶  state diff  ──▶  alerts (ntfy / Pushover)
```

### Module map

- `src/researcher/cli.py` — Click entry points. `_poll_once` is the orchestrator that drives one full cycle: iterate directional `(origin, destination)` tuples, ingest legs, re-join pairs, dispatch new alerts.
- `src/researcher/seatsaero.py` — Pro Partner API client (`/search` with skip/take pagination) and `normalize()` which flattens each cabin's per-row fields (`YAvailable`/`YMileageCost`/…) into one `LegRow` per `(row, cabin)`. Source/origin/destination casing is normalized (source lower, airports upper).
- `src/researcher/pairs.py` — `upsert_leg` (`ON CONFLICT` on the leg unique key) and `join_pairs`, which is where the core logic lives. `join_pairs` is also where invalidation happens: any previously-active pair *not present* in the current SQL join result gets flipped to `invalidated`.
- `src/researcher/pricing.py` — `pair_feasibility`. If both legs are the same program, mileage and fees are *summed* and assessed against that program's combined direct + transferable-pool balance. If programs differ, each leg is checked independently (two-ticket split) and only marked bookable if both pass. `BalancesConfig.effective_balance(program)` is the canonical "direct + every transferable pool that targets this program" calculation.
- `src/researcher/alerts.py` — `build_alerts_for_new_viable` selects pairs where `state='viable'` AND (never alerted OR `last_alerted_at < last_seen_at`). `mark_alerted` then flips them to `state='alerted'` so future cycles don't re-page.
- `src/researcher/db.py` — sqlite schema + `connect` (sets `foreign_keys=ON`, `journal_mode=WAL`, autocommit isolation) + `transaction()` context manager that explicitly `BEGIN`/`COMMIT`/`ROLLBACK`.
- `src/researcher/config.py` — frozen dataclasses + YAML/env loaders. `BalancesConfig.effective_balance` is the only behavioral method.

### State machine for pairs

```
candidate ──(feasible)──▶ viable ──(alert dispatched)──▶ alerted
    │                       │                                │
    └───────────────────────┴────(no longer in join)────────▶ invalidated
```

- `candidate`: pair satisfies the SQL join (seats, dates, nights, routing) but `pair_feasibility` says no funded pool covers it.
- `viable`: feasible but not yet alerted. **Only the `candidate → viable` and direct-insert-as-viable transitions emit alerts.**
- `alerted`: same as viable, but a notification already went out. Won't re-page unless legs change such that `last_seen_at > last_alerted_at` (currently this won't trigger because `mark_alerted` writes `last_alerted_at = now` after `last_seen_at` was set in the same cycle).
- `invalidated`: terminal-until-rejoined. If the underlying legs reappear and satisfy the join, the pair record (keyed on `out_leg_id, ret_leg_id`) is reused and transitions back via the `existing is not None` branch in `join_pairs`.

Every transition is appended to `pair_events` (`old_state, new_state, ts, note`) for auditability.

### Important invariants

- **Leg uniqueness** is `(source, origin, destination, depart_date, cabin)`. Re-polls update seats/miles/fees in place; the row id (and therefore any pair referencing it) is stable.
- **Pair uniqueness** is `(out_leg_id, ret_leg_id)`. A pair survives a re-poll as long as both legs survive.
- **Foreign keys are ON with `ON DELETE CASCADE`** — deleting a leg deletes every pair referencing it. The current code never deletes legs, but anything that does must account for this.
- **Nights math is done in SQL** via `CAST(julianday(r.depart_date) - julianday(o.depart_date) AS INTEGER)`. Don't move this to Python without preserving the int truncation semantics — the test suite relies on exact day counts.
- **Open-jaw filtering happens in Python** after the SQL join (see the `if not routing.allow_open_jaw` branch). The SQL join itself always permits open-jaw because origins/destinations are matched against the full sets.

### Where to add functionality

- New alert sink → add `_send_*` in `alerts.py`, branch in `dispatch`, add env field in `config.Env`.
- New seats.aero cabin or field → extend `CABIN_FIELD` in `seatsaero.py`.
- New feasibility rule (e.g., layover constraint at pair level) → extend `pair_feasibility` in `pricing.py`; the `Feasibility.reason` string flows into `pair_events.note` for debugging.
- New constraint that filters legs before they enter the pair join → either tighten the SQL in `join_pairs` or filter in `seatsaero.normalize` / `cli._poll_once`.

## Testing

Tests live in `tests/test_pairs.py` and run against a real sqlite database in `tmp_path`. The pattern is: build a minimal `SearchConfig` + `BalancesConfig` via helpers, `upsert_leg` directly, call `join_pairs`, assert on `JoinStats`. Add tests in this style — don't mock the database.
