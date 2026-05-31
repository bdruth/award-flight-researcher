from datetime import date

import httpx
import pytest

from researcher.seatsaero import BASE, RateLimited, SeatsAeroClient, normalize


def _row(*, j_direct: bool, j_seats: int = 4) -> dict:
    return {
        "ID": "abc123",
        "Date": "2026-07-01",
        "Source": "aeroplan",
        "Route": {"OriginAirport": "JFK", "DestinationAirport": "FRA"},
        "JAvailable": True,
        "JMileageCost": 60000,
        "JTotalTaxes": 12345,
        "JDirect": j_direct,
        "JRemainingSeats": j_seats,
    }


def test_normalize_default_keeps_indirect_legs():
    legs = list(normalize([_row(j_direct=False)], cabins=["business"], min_seats=2))
    assert len(legs) == 1
    assert legs[0].direct is False


def test_normalize_direct_only_drops_indirect_legs():
    legs = list(
        normalize([_row(j_direct=False)], cabins=["business"], min_seats=2, direct_only=True)
    )
    assert legs == []


def test_normalize_direct_only_keeps_direct_legs():
    legs = list(
        normalize([_row(j_direct=True)], cabins=["business"], min_seats=2, direct_only=True)
    )
    assert len(legs) == 1
    assert legs[0].direct is True


def _client_with_transport(handler):
    client = SeatsAeroClient("test-key")
    client._client = httpx.Client(
        base_url=BASE,
        headers={"Partner-Authorization": "test-key", "Accept": "application/json"},
        transport=httpx.MockTransport(handler),
    )
    return client


def test_cached_search_raises_rate_limited_on_429():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-RateLimit-Remaining": "0"}, text="rate limited")

    with _client_with_transport(handler) as client:
        with pytest.raises(RateLimited) as exc:
            client.cached_search(
                origin="ORD", destination="NRT", start=date(2026, 7, 1), end=date(2026, 7, 10)
            )
    assert exc.value.remaining == "0"


def test_trip_raises_rate_limited_on_429():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-RateLimit-Remaining": "0"})

    with _client_with_transport(handler) as client:
        with pytest.raises(RateLimited):
            client.trip("abc123")


def test_cached_search_returns_data_on_200():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-RateLimit-Remaining": "999"},
            json={"data": [{"ID": "x"}], "hasMore": False},
        )

    with _client_with_transport(handler) as client:
        out = client.cached_search(
            origin="ORD", destination="NRT", start=date(2026, 7, 1), end=date(2026, 7, 10)
        )
    assert out == [{"ID": "x"}]
