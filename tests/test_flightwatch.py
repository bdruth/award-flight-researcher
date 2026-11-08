from datetime import date
from pathlib import Path

import httpx
import pytest

from researcher import db as db_mod
from researcher import flightwatch as fw
from researcher.seatsaero import BASE, RateLimited, SeatsAeroClient

JL9 = fw.WatchedFlight(
    flight="JL9", origin="ORD", destination="HND", depart_date=date(2027, 1, 2),
    cabins=("business", "first"), note="1 seat → wife's AA account",
)
SOURCES = ("american", "alaska", "jal")


def _cfg(**kw) -> fw.WatchConfig:
    base = dict(flights=(JL9,), sources=SOURCES, max_miles={"jal": 100_000}, priority=1, notify_gone=True)
    base.update(kw)
    return fw.WatchConfig(**base)


def _trip(cabin: str, flights: str, *, seats: int = 2, miles: int = 60_000, origin="ORD", dest="HND") -> dict:
    return {
        "Cabin": cabin, "FlightNumbers": flights, "RemainingSeats": seats, "MileageCost": miles,
        "TotalTaxes": 560, "OriginAirport": origin, "DestinationAirport": dest,
        "AvailabilityID": f"av-{cabin}-{flights}", "UpdatedAt": "2026-12-28T10:00:00Z",
    }


def _row(source: str, *trips: dict, day: str = "2027-01-02") -> dict:
    return {"ID": f"row-{source}", "Source": source, "Date": day, "AvailabilityTrips": list(trips)}


@pytest.fixture
def conn(tmp_path: Path):
    c = db_mod.connect(tmp_path / "watch.db")
    db_mod.init(c)
    return c


def _check(conn, payload, cfg=None, now="2026-12-28T12:00:00Z"):
    cfg = cfg or _cfg()
    matches = fw.find_matches(payload, JL9, cfg.sources)
    return fw.reconcile(conn, JL9, cfg.sources, matches, cfg, now=now)


def test_normalize_flight_strips_zero_padding():
    assert fw.normalize_flight("JL009") == "JL9"
    assert fw.normalize_flight("jl 9") == "JL9"
    assert fw.normalize_flight("JL9") == "JL9"


def test_find_matches_requires_exact_nonstop_flight_date_and_cabin():
    payload = [
        _row("american",
             _trip("business", "JL9"),                 # match
             _trip("business", "AA2039, JL9"),         # connection: not our flight
             _trip("business", "JL99"),                # different flight
             _trip("economy", "JL9")),                 # cabin not watched
        _row("alaska", _trip("business", "JL009", seats=1, miles=55_000)),  # padded → match
        _row("american", _trip("business", "JL9"), day="2027-01-03"),        # wrong date
        _row("united", _trip("business", "JL9")),                            # source not watched
    ]
    m = fw.find_matches(payload, JL9, SOURCES)
    assert set(m) == {("business", "american"), ("business", "alaska")}
    assert m[("business", "alaska")].miles == 55_000


def test_open_alerts_once_then_stays_quiet(conn):
    payload = [_row("american", _trip("business", "JL9", seats=1))]
    a = _check(conn, payload)
    assert a is not None and "OPEN" in a.title and a.priority == 1
    assert "aa.com" in a.url
    assert "wife" in a.body
    assert _check(conn, payload, now="2026-12-28T13:00:00Z") is None


def test_more_seats_realerts_but_fewer_does_not(conn):
    _check(conn, [_row("american", _trip("business", "JL9", seats=1))])
    a = _check(conn, [_row("american", _trip("business", "JL9", seats=3))], now="2026-12-28T13:00:00Z")
    assert a is not None and "more seats" in a.title and "1 seat → 3 seats" in a.body
    assert _check(conn, [_row("american", _trip("business", "JL9", seats=2))], now="2026-12-28T14:00:00Z") is None


def test_gone_is_quiet_and_reopen_alerts_again(conn):
    _check(conn, [_row("american", _trip("business", "JL9"))])
    gone = _check(conn, [], now="2026-12-28T13:00:00Z")
    assert gone is not None and gone.priority == -1 and "GONE" in gone.body
    assert _check(conn, [], now="2026-12-28T14:00:00Z") is None
    again = _check(conn, [_row("american", _trip("business", "JL9"))], now="2026-12-28T15:00:00Z")
    assert again is not None and "OPEN" in again.title


def test_notify_gone_false_suppresses_gone_alert(conn):
    cfg = _cfg(notify_gone=False)
    _check(conn, [_row("american", _trip("business", "JL9"))], cfg=cfg)
    assert _check(conn, [], cfg=cfg, now="2026-12-28T13:00:00Z") is None
    row = conn.execute("SELECT available FROM flight_watch").fetchone()
    assert row["available"] == 0


def test_mileage_cap_filters_per_source(conn):
    over = [_row("jal", _trip("business", "JL9", seats=1, miles=135_000))]
    assert _check(conn, over) is None
    assert conn.execute("SELECT COUNT(*) FROM flight_watch").fetchone()[0] == 0
    under = [_row("jal", _trip("business", "JL9", seats=1, miles=95_000))]
    a = _check(conn, under, now="2026-12-28T13:00:00Z")
    assert a is not None and "JAL Mileage Bank" in a.body
    assert "seats.aero" in a.url  # not AA partner space → seats.aero link


def test_run_check_skips_departed_and_survives_search_errors(conn):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        if request.url.params["origin_airport"] == "NRT":
            return httpx.Response(500)
        return httpx.Response(200, json={"data": [_row("american", _trip("business", "JL9"))], "hasMore": False})

    jl56 = fw.WatchedFlight("JL56", "NRT", "ORD", date(2027, 1, 21), ("business",))
    departed = fw.WatchedFlight("JL10", "HND", "ORD", date(2026, 12, 1), ("business",))
    client = SeatsAeroClient("k")
    client._client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(handler))
    alerts = fw.run_check(conn, client, _cfg(flights=(JL9, jl56, departed)), today=date(2026, 12, 28))
    assert len(alerts) == 1 and alerts[0].title.startswith("JL9")
    assert [c["origin_airport"] for c in calls] == ["ORD", "NRT"]
    assert calls[0]["include_trips"] == "true"
    assert calls[0]["start_date"] == calls[0]["end_date"] == "2027-01-02"


def test_run_check_propagates_rate_limit(conn):
    client = SeatsAeroClient("k")
    client._client = httpx.Client(
        base_url=BASE, transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"X-RateLimit-Remaining": "0"}))
    )
    with pytest.raises(RateLimited):
        fw.run_check(conn, client, _cfg(), today=date(2026, 12, 28))


def test_load_watch(tmp_path: Path):
    p = tmp_path / "watch.yaml"
    p.write_text(
        "sources: [american, jal]\n"
        "max_miles: {jal: 100000}\n"
        "priority: 2\n"
        "flights:\n"
        "  - {flight: JL009, origin: ord, destination: hnd, date: 2027-01-02, cabins: [business], note: hi}\n"
    )
    cfg = fw.load_watch(p)
    assert cfg.sources == ("american", "jal") and cfg.priority == 2 and cfg.max_miles == {"jal": 100_000}
    f = cfg.flights[0]
    assert (f.flight, f.origin, f.destination, f.depart_date, f.cabins, f.note) == (
        "JL9", "ORD", "HND", date(2027, 1, 2), ("business",), "hi",
    )
