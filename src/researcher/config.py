from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date
    nights_min: int
    nights_max: int


@dataclass(frozen=True)
class TripConfig:
    passengers: int
    windows: tuple[DateWindow, ...]


@dataclass(frozen=True)
class RoutingConfig:
    origins: tuple[str, ...]
    destinations: tuple[str, ...]
    allow_open_jaw: bool
    max_stops: int
    layover_min: int
    layover_max: int


@dataclass(frozen=True)
class PollConfig:
    cached_interval_minutes: int
    live_interval_minutes: int
    hot_window_days: int
    hot_interval_minutes: int


@dataclass(frozen=True)
class LegFilters:
    min_seats: int
    exclude_overnight_layovers: bool


@dataclass(frozen=True)
class SearchConfig:
    trip: TripConfig
    routing: RoutingConfig
    cabins: tuple[str, ...]
    sources: tuple[str, ...]
    poll: PollConfig
    leg_filters: LegFilters


@dataclass(frozen=True)
class TransferablePool:
    name: str
    balance: int
    targets: tuple[str, ...]


@dataclass(frozen=True)
class BalancesConfig:
    direct: dict[str, int]
    transferable: tuple[TransferablePool, ...]
    max_fees_per_pax_usd: int
    max_miles_per_pax: dict[str, int]

    def effective_balance(self, program: str) -> int:
        direct = self.direct.get(program, 0)
        transfer = sum(p.balance for p in self.transferable if program in p.targets)
        return direct + transfer


@dataclass(frozen=True)
class Env:
    seatsaero_api_key: str
    ntfy_topic: str | None
    ntfy_server: str
    pushover_token: str | None
    pushover_user: str | None
    db_path: Path
    log_level: str


def _to_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def load_search(path: Path) -> SearchConfig:
    data = yaml.safe_load(path.read_text())
    t = data["trip"]
    r = data["routing"]
    p = data["poll"]
    lf = data["leg_filters"]
    windows = tuple(
        DateWindow(
            start=_to_date(w["start"]),
            end=_to_date(w["end"]),
            nights_min=int(w["nights"]["min"]),
            nights_max=int(w["nights"]["max"]),
        )
        for w in t["windows"]
    )
    return SearchConfig(
        trip=TripConfig(passengers=int(t["passengers"]), windows=windows),
        routing=RoutingConfig(
            origins=tuple(r["origins"]),
            destinations=tuple(r["destinations"]),
            allow_open_jaw=bool(r.get("allow_open_jaw", True)),
            max_stops=int(r.get("max_stops", 1)),
            layover_min=int(r["layover_minutes"]["min"]),
            layover_max=int(r["layover_minutes"]["max"]),
        ),
        cabins=tuple(data["cabins"]),
        sources=tuple(data["sources"]),
        poll=PollConfig(
            cached_interval_minutes=int(p["cached_interval_minutes"]),
            live_interval_minutes=int(p["live_interval_minutes"]),
            hot_window_days=int(p["hot_window_days"]),
            hot_interval_minutes=int(p["hot_interval_minutes"]),
        ),
        leg_filters=LegFilters(
            min_seats=int(lf["min_seats"]),
            exclude_overnight_layovers=bool(lf.get("exclude_overnight_layovers", False)),
        ),
    )


def load_balances(path: Path) -> BalancesConfig:
    data = yaml.safe_load(path.read_text())
    pools = tuple(
        TransferablePool(name=name, balance=int(d["balance"]), targets=tuple(d["targets"]))
        for name, d in data.get("transferable", {}).items()
    )
    limits = data.get("limits", {})
    return BalancesConfig(
        direct={k: int(v) for k, v in data.get("direct", {}).items()},
        transferable=pools,
        max_fees_per_pax_usd=int(limits.get("max_fees_per_pax_usd", 10**6)),
        max_miles_per_pax={k: int(v) for k, v in limits.get("max_miles_per_pax", {}).items()},
    )


def load_env() -> Env:
    load_dotenv()
    api_key = os.environ.get("SEATSAERO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("SEATSAERO_API_KEY is required (see .env.example)")
    return Env(
        seatsaero_api_key=api_key,
        ntfy_topic=os.environ.get("NTFY_TOPIC") or None,
        ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
        pushover_token=os.environ.get("PUSHOVER_TOKEN") or None,
        pushover_user=os.environ.get("PUSHOVER_USER") or None,
        db_path=Path(os.environ.get("RESEARCHER_DB_PATH", "data/researcher.db")),
        log_level=os.environ.get("RESEARCHER_LOG_LEVEL", "INFO"),
    )
