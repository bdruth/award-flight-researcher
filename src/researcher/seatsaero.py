from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Iterator

import httpx

log = logging.getLogger(__name__)

BASE = "https://seats.aero/partnerapi"

CABIN_FIELD = {
    "economy": ("Y", "YAvailable", "YMileageCost", "YTotalTaxes", "YDirect", "YRemainingSeats"),
    "premium": ("W", "WAvailable", "WMileageCost", "WTotalTaxes", "WDirect", "WRemainingSeats"),
    "business": ("J", "JAvailable", "JMileageCost", "JTotalTaxes", "JDirect", "JRemainingSeats"),
    "first": ("F", "FAvailable", "FMileageCost", "FTotalTaxes", "FDirect", "FRemainingSeats"),
}


@dataclass(frozen=True)
class LegRow:
    source: str
    origin: str
    destination: str
    depart_date: date
    cabin: str
    seats_remaining: int
    miles: int
    fees_cents: int
    direct: bool
    raw: dict
    availability_id: str | None = None


class SeatsAeroClient:
    def __init__(self, api_key: str, timeout: float = 30.0):
        self._client = httpx.Client(
            base_url=BASE,
            headers={"Partner-Authorization": api_key, "Accept": "application/json"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SeatsAeroClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def trip(self, availability_id: str) -> list[dict]:
        """Fetch trip variants (each with per-segment detail) for an availability."""
        r = self._client.get(f"/trips/{availability_id}")
        r.raise_for_status()
        return r.json().get("data", []) or []

    def cached_search(
        self,
        *,
        origin: str,
        destination: str,
        start: date,
        end: date,
        sources: Iterable[str] | None = None,
        take: int = 1000,
    ) -> list[dict]:
        params: dict[str, str | int] = {
            "origin_airport": origin,
            "destination_airport": destination,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "take": take,
            "order_by": "lowest_mileage",
        }
        if sources:
            params["sources"] = ",".join(sources)
        out: list[dict] = []
        skip = 0
        while True:
            params["skip"] = skip
            r = self._client.get("/search", params=params)
            r.raise_for_status()
            payload = r.json()
            batch = payload.get("data", [])
            out.extend(batch)
            if not payload.get("hasMore") or not batch:
                break
            skip += len(batch)
        return out


def normalize(
    payload: list[dict],
    *,
    cabins: Iterable[str],
    min_seats: int,
) -> Iterator[LegRow]:
    """Flatten seats.aero search rows into one LegRow per (row, cabin)."""
    for row in payload:
        depart = _parse_date(row.get("Date") or row.get("date"))
        if depart is None:
            continue
        source = row.get("Source") or row.get("source") or ""
        origin = row.get("Route", {}).get("OriginAirport") or row.get("OriginAirport") or ""
        dest = row.get("Route", {}).get("DestinationAirport") or row.get("DestinationAirport") or ""
        if not (source and origin and dest):
            continue
        for cabin in cabins:
            fields = CABIN_FIELD.get(cabin)
            if not fields:
                continue
            _, avail_f, miles_f, taxes_f, direct_f, seats_f = fields
            if not row.get(avail_f):
                continue
            seats = int(row.get(seats_f) or 0)
            if seats < min_seats:
                continue
            miles = int(row.get(miles_f) or 0)
            if miles <= 0:
                continue
            fees_cents = _taxes_to_cents(row.get(taxes_f))
            avail_id = row.get("ID") or row.get("Id") or row.get("id")
            yield LegRow(
                source=str(source).lower(),
                origin=str(origin).upper(),
                destination=str(dest).upper(),
                depart_date=depart,
                cabin=cabin,
                seats_remaining=seats,
                miles=miles,
                fees_cents=fees_cents,
                direct=bool(row.get(direct_f)),
                raw=row,
                availability_id=str(avail_id) if avail_id else None,
            )


def _parse_date(v) -> date | None:
    if not v:
        return None
    s = str(v)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def trip_layovers_minutes(trip: dict) -> list[int]:
    """Connection times in minutes between consecutive segments of one trip.
    Empty for direct (single-segment) trips."""
    segs = sorted(trip.get("AvailabilitySegments") or [], key=lambda s: s.get("Order", 0))
    out: list[int] = []
    for i in range(len(segs) - 1):
        arr = _parse_iso(segs[i].get("ArrivesAt"))
        dep = _parse_iso(segs[i + 1].get("DepartsAt"))
        if arr is None or dep is None:
            continue
        out.append(int((dep - arr).total_seconds() // 60))
    return out


def any_trip_within_layover_window(
    trips: list[dict], *, layover_min: int, layover_max: int
) -> bool:
    """True if at least one trip variant has every layover in [min, max].
    Direct trips (no layovers) always pass."""
    for trip in trips:
        gaps = trip_layovers_minutes(trip)
        if not gaps:
            return True
        if all(layover_min <= g <= layover_max for g in gaps):
            return True
    return False


def availability_url(*, origin: str, destination: str, depart_date: date, source: str) -> str:
    """Front-end search URL that scopes results to one route+date+source."""
    iso = depart_date.isoformat()
    return (
        "https://seats.aero/search"
        f"?origin_airport={origin}&destination_airport={destination}"
        f"&start_date={iso}&end_date={iso}&source={source}"
    )


def _parse_iso(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _taxes_to_cents(v) -> int:
    """seats.aero returns taxes already in cents (int or decimal-free string).
    A string with a decimal point is dollars (e.g. '125.40') and gets converted."""
    if v is None or v == "":
        return 0
    try:
        if isinstance(v, (int, float)):
            return int(v)
        s = str(v).replace(",", "").strip()
        if "." in s:
            return int(round(float(s) * 100))
        return int(s)
    except (ValueError, TypeError):
        return 0
