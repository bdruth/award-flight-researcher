from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
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
            )


def _parse_date(v) -> date | None:
    if not v:
        return None
    s = str(v)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _taxes_to_cents(v) -> int:
    """seats.aero returns taxes as a string like '125.40' or as cents int; normalize to cents."""
    if v is None or v == "":
        return 0
    try:
        if isinstance(v, (int, float)):
            return int(round(float(v) * 100)) if float(v) < 10000 else int(v)
        s = str(v).replace(",", "").strip()
        if "." in s:
            return int(round(float(s) * 100))
        n = int(s)
        return n if n > 10000 else n * 100
    except (ValueError, TypeError):
        return 0
