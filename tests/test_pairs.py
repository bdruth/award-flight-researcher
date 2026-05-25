from datetime import date
from pathlib import Path

import pytest

from researcher import alerts as alerts_mod
from researcher import db as db_mod
from researcher import pairs as pairs_mod
from researcher.config import (
    BalancesConfig,
    LegFilters,
    PollConfig,
    RoutingConfig,
    SearchConfig,
    TransferablePool,
    TripConfig,
)
from researcher.seatsaero import LegRow, _taxes_to_cents


def _search(allow_open_jaw: bool = True, cabins: tuple[str, ...] = ("economy",)) -> SearchConfig:
    return SearchConfig(
        trip=TripConfig(
            passengers=4,
            nights_min=13,
            nights_max=15,
            window_start=date(2026, 12, 15),
            window_end=date(2027, 1, 15),
        ),
        routing=RoutingConfig(
            origins=("ORD",),
            destinations=("HND", "NRT"),
            allow_open_jaw=allow_open_jaw,
            max_stops=1,
            layover_min=90,
            layover_max=300,
        ),
        cabins=cabins,
        sources=("aeroplan",),
        poll=PollConfig(15, 360, 30, 5),
        leg_filters=LegFilters(min_seats=4, exclude_overnight_layovers=False),
    )


def _balances(direct: int = 1_000_000, max_miles_per_pax: dict[str, int] | None = None) -> BalancesConfig:
    return BalancesConfig(
        direct={"aeroplan": direct},
        transferable=(TransferablePool(name="chase_ur", balance=0, targets=("aeroplan",)),),
        max_fees_per_pax_usd=300,
        max_miles_per_pax=max_miles_per_pax or {"economy": 150_000, "business": 250_000},
    )


def _leg(origin: str, dest: str, d: date, *, source="aeroplan", seats=4, miles=55_000, fees_cents=8000, cabin="economy") -> LegRow:
    return LegRow(
        source=source, origin=origin, destination=dest, depart_date=d,
        cabin=cabin, seats_remaining=seats, miles=miles, fees_cents=fees_cents,
        direct=True, raw={},
    )


@pytest.fixture()
def conn(tmp_path: Path):
    c = db_mod.connect(tmp_path / "t.db")
    db_mod.init(c)
    yield c
    c.close()


def test_pair_within_night_window_promotes_viable(conn):
    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18)))
    pairs_mod.upsert_leg(conn, _leg("HND", "ORD", date(2027, 1, 1)))   # 14 nights
    stats = pairs_mod.join_pairs(conn, _search(), _balances())
    assert stats.pairs_new == 1
    assert stats.pairs_promoted_viable == 1


def test_pair_outside_night_window_excluded(conn):
    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18)))
    pairs_mod.upsert_leg(conn, _leg("HND", "ORD", date(2026, 12, 25)))  # 7 nights
    stats = pairs_mod.join_pairs(conn, _search(), _balances())
    assert stats.pairs_seen == 0


def test_open_jaw_pair_visible_when_allowed(conn):
    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18)))
    pairs_mod.upsert_leg(conn, _leg("NRT", "ORD", date(2027, 1, 1)))
    stats = pairs_mod.join_pairs(conn, _search(allow_open_jaw=True), _balances())
    assert stats.pairs_new == 1


def test_open_jaw_pair_hidden_when_disallowed(conn):
    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18)))
    pairs_mod.upsert_leg(conn, _leg("NRT", "ORD", date(2027, 1, 1)))
    stats = pairs_mod.join_pairs(conn, _search(allow_open_jaw=False), _balances())
    assert stats.pairs_seen == 0


def test_insufficient_seats_excluded(conn):
    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18), seats=3))
    pairs_mod.upsert_leg(conn, _leg("HND", "ORD", date(2027, 1, 1)))
    stats = pairs_mod.join_pairs(conn, _search(), _balances())
    assert stats.pairs_seen == 0


def test_unfunded_pair_stays_candidate(conn):
    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18), miles=55_000))
    pairs_mod.upsert_leg(conn, _leg("HND", "ORD", date(2027, 1, 1), miles=55_000))
    # 4 pax * 110k = 440k required. Give only 100k.
    stats = pairs_mod.join_pairs(conn, _search(), _balances(direct=100_000))
    assert stats.pairs_new == 1
    assert stats.pairs_promoted_viable == 0


def test_invalidation_on_seat_loss(conn):
    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18)))
    pairs_mod.upsert_leg(conn, _leg("HND", "ORD", date(2027, 1, 1)))
    pairs_mod.join_pairs(conn, _search(), _balances())

    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18), seats=1))
    stats = pairs_mod.join_pairs(conn, _search(), _balances())
    assert stats.pairs_invalidated == 1


def test_taxes_to_cents_trusts_int_as_cents():
    # Regression: seats.aero returns YTotalTaxes as int cents (e.g. 560 for $5.60).
    # The earlier "small int = dollars" heuristic was multiplying these by 100.
    assert _taxes_to_cents(560) == 560
    assert _taxes_to_cents(12345) == 12345
    assert _taxes_to_cents("560") == 560
    assert _taxes_to_cents("5.60") == 560        # decimal-bearing string is dollars
    assert _taxes_to_cents("125.40") == 12540
    assert _taxes_to_cents(None) == 0
    assert _taxes_to_cents("") == 0


def test_mixed_cabin_pair_promoted_viable(conn):
    # business outbound + economy return on the same program: caps are checked
    # per-leg against each leg's own cabin cap, balance is summed across the pair.
    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18), cabin="business", miles=120_000))
    pairs_mod.upsert_leg(conn, _leg("HND", "ORD", date(2027, 1, 1),  cabin="economy",  miles=55_000))
    stats = pairs_mod.join_pairs(conn, _search(cabins=("economy", "business")), _balances())
    assert stats.pairs_new == 1
    assert stats.pairs_promoted_viable == 1


def test_mixed_cabin_pair_blocked_by_per_leg_cap(conn):
    # outbound business miles (260k) exceed business cap (250k); the cheap
    # economy return must not rescue the pair via averaging.
    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18), cabin="business", miles=260_000))
    pairs_mod.upsert_leg(conn, _leg("HND", "ORD", date(2027, 1, 1),  cabin="economy",  miles=55_000))
    stats = pairs_mod.join_pairs(conn, _search(cabins=("economy", "business")), _balances())
    assert stats.pairs_new == 1
    assert stats.pairs_promoted_viable == 0


def test_alerted_pair_stays_alerted_on_rejoin(conn):
    # Regression: re-joining a still-feasible pair previously overwrote its
    # 'alerted' state with 'viable', causing it to re-page every cycle.
    pairs_mod.upsert_leg(conn, _leg("ORD", "HND", date(2026, 12, 18)))
    pairs_mod.upsert_leg(conn, _leg("HND", "ORD", date(2027, 1, 1)))
    pairs_mod.join_pairs(conn, _search(), _balances())

    pending = alerts_mod.build_alerts_for_new_viable(conn)
    assert len(pending) == 1
    alerts_mod.mark_alerted(conn, [a.pair_id for a in pending])

    # Re-join: same legs, still feasible. Must not re-queue the alert.
    stats = pairs_mod.join_pairs(conn, _search(), _balances())
    assert stats.pairs_promoted_viable == 0
    assert alerts_mod.build_alerts_for_new_viable(conn) == []

    state = conn.execute("SELECT state FROM pairs").fetchone()[0]
    assert state == "alerted"
