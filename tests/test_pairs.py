from datetime import date
from pathlib import Path

import pytest

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
from researcher.seatsaero import LegRow


def _search(allow_open_jaw: bool = True) -> SearchConfig:
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
        cabins=("economy",),
        sources=("aeroplan",),
        poll=PollConfig(15, 360, 30, 5),
        leg_filters=LegFilters(min_seats=4, exclude_overnight_layovers=False),
    )


def _balances(direct: int = 1_000_000) -> BalancesConfig:
    return BalancesConfig(
        direct={"aeroplan": direct},
        transferable=(TransferablePool(name="chase_ur", balance=0, targets=("aeroplan",)),),
        max_fees_per_pax_usd=300,
        max_miles_per_pax={"economy": 150_000},
    )


def _leg(origin: str, dest: str, d: date, *, source="aeroplan", seats=4, miles=55_000, fees_cents=8000) -> LegRow:
    return LegRow(
        source=source, origin=origin, destination=dest, depart_date=d,
        cabin="economy", seats_remaining=seats, miles=miles, fees_cents=fees_cents,
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
