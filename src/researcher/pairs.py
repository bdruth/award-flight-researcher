from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import BalancesConfig, SearchConfig
from .pricing import best_pax_split, pair_feasibility

ISO = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(ISO)


@dataclass(frozen=True)
class JoinStats:
    pairs_seen: int
    pairs_new: int
    pairs_promoted_viable: int
    pairs_invalidated: int


def upsert_leg(conn: sqlite3.Connection, leg) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO legs (source, origin, destination, depart_date, cabin,
                          seats_remaining, miles, fees_cents, stops, direct,
                          duration_min, flight_numbers,
                          first_seen_at, last_seen_at, last_snapshot_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, origin, destination, depart_date, cabin) DO UPDATE SET
            seats_remaining = excluded.seats_remaining,
            miles = excluded.miles,
            fees_cents = excluded.fees_cents,
            direct = excluded.direct,
            last_seen_at = excluded.last_seen_at,
            last_snapshot_json = excluded.last_snapshot_json
        RETURNING id
        """,
        (
            leg.source, leg.origin, leg.destination, leg.depart_date.isoformat(), leg.cabin,
            leg.seats_remaining, leg.miles, leg.fees_cents,
            0 if leg.direct else 1, 1 if leg.direct else 0,
            None, None,
            now, now, None,
        ),
    )
    row = cur.fetchone()
    return int(row[0])


def join_pairs(
    conn: sqlite3.Connection,
    search: SearchConfig,
    balances: BalancesConfig,
) -> JoinStats:
    """Re-derive viable pairs from current legs. Returns transitions to act on."""
    trip = search.trip
    routing = search.routing
    pax = trip.passengers
    now = _now()

    # Candidate join: same cabin, fresh enough, both have ≥ pax seats, nights ∈ window
    # We delegate the nights math to SQL via julianday().
    out_dests = tuple(routing.destinations)
    in_origins = tuple(routing.destinations)
    origins = tuple(routing.origins)

    placeholders_o = ",".join("?" * len(origins))
    placeholders_d = ",".join("?" * len(out_dests))

    rows = conn.execute(
        f"""
        SELECT
            o.id  AS out_id,
            r.id  AS ret_id,
            o.cabin AS out_cabin,
            r.cabin AS ret_cabin,
            o.source AS out_source,
            r.source AS ret_source,
            o.miles AS out_miles,
            r.miles AS ret_miles,
            o.fees_cents AS out_fees,
            r.fees_cents AS ret_fees,
            o.origin AS out_origin,
            o.destination AS out_dest,
            r.origin AS ret_origin,
            r.destination AS ret_dest,
            o.depart_date AS out_date,
            r.depart_date AS ret_date,
            CAST(julianday(r.depart_date) - julianday(o.depart_date) AS INTEGER) AS nights
        FROM legs o
        JOIN legs r
          ON o.origin IN ({placeholders_o})
         AND o.destination IN ({placeholders_d})
         AND r.origin IN ({placeholders_d})
         AND r.destination IN ({placeholders_o})
         AND o.seats_remaining >= ?
         AND r.seats_remaining >= ?
         AND date(o.depart_date) BETWEEN date(?) AND date(?)
         AND date(r.depart_date) BETWEEN date(?) AND date(?)
         AND CAST(julianday(r.depart_date) - julianday(o.depart_date) AS INTEGER)
             BETWEEN ? AND ?
        """,
        (
            *origins, *out_dests, *out_dests, *origins,
            pax, pax,
            trip.window_start.isoformat(), trip.window_end.isoformat(),
            trip.window_start.isoformat(), trip.window_end.isoformat(),
            trip.nights_min, trip.nights_max,
        ),
    ).fetchall()

    # If open_jaw is disallowed, require out_origin == ret_dest AND out_dest == ret_origin
    if not routing.allow_open_jaw:
        rows = [r for r in rows if r["out_origin"] == r["ret_dest"] and r["out_dest"] == r["ret_origin"]]

    stats = {"pairs_seen": 0, "pairs_new": 0, "pairs_promoted_viable": 0, "pairs_invalidated": 0}

    seen_pair_ids: set[int] = set()

    for row in rows:
        stats["pairs_seen"] += 1
        feas = pair_feasibility(
            out_program=row["out_source"],
            ret_program=row["ret_source"],
            out_miles=row["out_miles"],
            ret_miles=row["ret_miles"],
            out_fees_cents=row["out_fees"],
            ret_fees_cents=row["ret_fees"],
            passengers=pax,
            out_cabin=row["out_cabin"],
            ret_cabin=row["ret_cabin"],
            balances=balances,
        )
        new_state = "viable" if feas.bookable else "candidate"

        total_miles = (row["out_miles"] + row["ret_miles"]) * pax
        total_fees = (row["out_fees"] + row["ret_fees"]) * pax

        existing = conn.execute(
            "SELECT id, state FROM pairs WHERE out_leg_id = ? AND ret_leg_id = ?",
            (row["out_id"], row["ret_id"]),
        ).fetchone()

        if existing is None:
            cur = conn.execute(
                """
                INSERT INTO pairs (out_leg_id, ret_leg_id, nights, total_miles, total_fees_cents,
                                   bookable_from, state, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (row["out_id"], row["ret_id"], row["nights"], total_miles, total_fees,
                 feas.pool_name, new_state, now, now),
            )
            pid = int(cur.fetchone()[0])
            conn.execute(
                "INSERT INTO pair_events (pair_id, old_state, new_state, ts, note) VALUES (?, ?, ?, ?, ?)",
                (pid, None, new_state, now, feas.reason),
            )
            stats["pairs_new"] += 1
            if new_state == "viable":
                stats["pairs_promoted_viable"] += 1
            seen_pair_ids.add(pid)
        else:
            pid = int(existing["id"])
            old_state = existing["state"]
            # 'alerted' is sticky as long as the pair stays feasible: re-seeing a
            # bookable pair must not put it back in the alert queue.
            effective_state = "alerted" if (old_state == "alerted" and new_state == "viable") else new_state
            conn.execute(
                """
                UPDATE pairs
                   SET nights = ?, total_miles = ?, total_fees_cents = ?,
                       bookable_from = ?, state = ?, last_seen_at = ?
                 WHERE id = ?
                """,
                (row["nights"], total_miles, total_fees, feas.pool_name, effective_state, now, pid),
            )
            if old_state != effective_state:
                conn.execute(
                    "INSERT INTO pair_events (pair_id, old_state, new_state, ts, note) VALUES (?, ?, ?, ?, ?)",
                    (pid, old_state, effective_state, now, feas.reason),
                )
                if effective_state == "viable":
                    stats["pairs_promoted_viable"] += 1
            seen_pair_ids.add(pid)

    # Invalidate previously-viable/candidate pairs no longer present in this join.
    if seen_pair_ids:
        placeholders = ",".join("?" * len(seen_pair_ids))
        invalidated = conn.execute(
            f"""
            SELECT id, state FROM pairs
             WHERE state IN ('candidate','viable','alerted')
               AND id NOT IN ({placeholders})
            """,
            tuple(seen_pair_ids),
        ).fetchall()
    else:
        invalidated = conn.execute(
            "SELECT id, state FROM pairs WHERE state IN ('candidate','viable','alerted')"
        ).fetchall()
    for inv in invalidated:
        conn.execute("UPDATE pairs SET state = 'invalidated', last_seen_at = ? WHERE id = ?",
                     (now, int(inv["id"])))
        conn.execute(
            "INSERT INTO pair_events (pair_id, old_state, new_state, ts, note) VALUES (?, ?, ?, ?, ?)",
            (int(inv["id"]), inv["state"], "invalidated", now, "no longer satisfies join"),
        )
        stats["pairs_invalidated"] += 1

    return JoinStats(**stats)


MIXED_CABIN = "mixed"


def synthesize_pax_splits(
    conn: sqlite3.Connection, *, pax: int, cabins: tuple[str, ...], balances: BalancesConfig
) -> int:
    """For each flight (source, origin, destination, depart_date) where no single
    cabin has >= pax seats but combined cabins do, upsert a synthetic 'mixed'
    leg representing the cheapest valid pax-split. Returns the count of mixed
    legs upserted. The pair joiner picks these up like any other cabin."""
    if pax <= 0 or not cabins:
        return 0
    now = _now()
    placeholders = ",".join("?" * len(cabins))

    flights = conn.execute(
        f"""
        SELECT source, origin, destination, depart_date,
               SUM(seats_remaining) AS total_seats,
               MAX(seats_remaining) AS max_cabin_seats
          FROM legs
         WHERE cabin IN ({placeholders})
         GROUP BY source, origin, destination, depart_date
        HAVING total_seats >= ? AND max_cabin_seats < ?
        """,
        (*cabins, pax, pax),
    ).fetchall()

    upserted = 0
    for f in flights:
        offer_rows = conn.execute(
            f"""
            SELECT cabin, seats_remaining, miles, fees_cents
              FROM legs
             WHERE source = ? AND origin = ? AND destination = ? AND depart_date = ?
               AND cabin IN ({placeholders})
            """,
            (f["source"], f["origin"], f["destination"], f["depart_date"], *cabins),
        ).fetchall()
        offers = [(o["cabin"], o["seats_remaining"], o["miles"], o["fees_cents"]) for o in offer_rows]
        split = best_pax_split(offers, pax, balances)
        if split is None:
            continue
        avg_miles = split.total_miles // pax
        avg_fees = split.total_fees_cents // pax
        snapshot = json.dumps({
            "pax": pax,
            "allocations": [
                {"cabin": a.cabin, "count": a.count, "miles_per_pax": a.miles_per_pax, "fees_cents_per_pax": a.fees_cents_per_pax}
                for a in split.allocations
            ],
            "total_miles": split.total_miles,
            "total_fees_cents": split.total_fees_cents,
        })
        conn.execute(
            """
            INSERT INTO legs (source, origin, destination, depart_date, cabin,
                              seats_remaining, miles, fees_cents, stops, direct,
                              duration_min, flight_numbers,
                              first_seen_at, last_seen_at, last_snapshot_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, NULL, NULL, ?, ?, ?)
            ON CONFLICT(source, origin, destination, depart_date, cabin) DO UPDATE SET
                seats_remaining = excluded.seats_remaining,
                miles = excluded.miles,
                fees_cents = excluded.fees_cents,
                last_seen_at = excluded.last_seen_at,
                last_snapshot_json = excluded.last_snapshot_json
            """,
            (f["source"], f["origin"], f["destination"], f["depart_date"], MIXED_CABIN,
             pax, avg_miles, avg_fees, now, now, snapshot),
        )
        upserted += 1
    return upserted
