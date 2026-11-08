"""Specific-flight watch: alert when award space opens on one exact flight + date.

The pair pipeline answers "is there a trip worth booking?". This answers a narrower
question for trips already booked: "has premium-cabin award space opened on the
flight I'm already on?" — so an existing economy award can be swapped (book the new
award first, then cancel the old one).

One seats.aero /search call per watched flight per check (with `include_trips` so the
flight numbers come back inline). State lives in the `flight_watch` table; alerts fire
when a (flight, date, cabin, program) starts qualifying, or its seat count rises, and
(optionally, quietly) when it stops qualifying.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import yaml

from . import seatsaero
from .alerts import Alert

log = logging.getLogger(__name__)
ISO = "%Y-%m-%dT%H:%M:%SZ"

PROGRAM_LABEL = {
    "american": "AAdvantage",
    "alaska": "Alaska Atmos",
    "qantas": "Qantas",
    "jal": "JAL Mileage Bank",
    "aeroplan": "Aeroplan",
    "united": "United",
    "qatar": "Qatar Avios",
    "virginatlantic": "Virgin Atlantic",
}

# oneworld programs whose partner space on JL/AA flights is (almost always) the same
# inventory AAdvantage sees — when one of these shows it, aa.com is worth checking.
AA_PARTNER_SPACE = {"american", "alaska", "qantas"}

CABIN_ORDER = ("first", "business", "premium", "economy")


@dataclass(frozen=True)
class WatchedFlight:
    flight: str
    origin: str
    destination: str
    depart_date: date
    cabins: tuple[str, ...]
    sources: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class WatchConfig:
    flights: tuple[WatchedFlight, ...]
    sources: tuple[str, ...]
    max_miles: dict[str, int] = field(default_factory=dict)
    priority: int = 1
    notify_gone: bool = True


@dataclass(frozen=True)
class Match:
    source: str
    cabin: str
    seats: int  # 0 = program doesn't report a count (common for AAdvantage)
    miles: int
    fees_cents: int
    availability_id: str | None
    updated_at: str | None


def load_watch(path: Path) -> WatchConfig:
    data = yaml.safe_load(path.read_text()) or {}
    default_sources = tuple(s.lower() for s in data.get("sources", ["american"]))
    flights = []
    for f in data.get("flights", []) or []:
        flights.append(
            WatchedFlight(
                flight=normalize_flight(str(f["flight"])),
                origin=str(f["origin"]).upper(),
                destination=str(f["destination"]).upper(),
                depart_date=seatsaero._parse_date(f["date"]) or date.fromisoformat(str(f["date"])),
                cabins=tuple(c.lower() for c in f.get("cabins", ["business", "first"])),
                sources=tuple(s.lower() for s in f.get("sources", []) or []),
                note=f.get("note"),
            )
        )
    return WatchConfig(
        flights=tuple(flights),
        sources=default_sources,
        max_miles={k.lower(): int(v) for k, v in (data.get("max_miles") or {}).items()},
        priority=int(data.get("priority", 1)),
        notify_gone=bool(data.get("notify_gone", True)),
    )


_FLIGHT_RE = re.compile(r"^([A-Z0-9]{2})\s*0*(\d+)$")


def normalize_flight(s: str) -> str:
    """'JL009' / 'jl 9' / 'JL9' → 'JL9' (seats.aero sources disagree on zero-padding)."""
    s = s.strip().upper()
    m = _FLIGHT_RE.match(s)
    return f"{m.group(1)}{m.group(2)}" if m else s


def find_matches(payload: list[dict], watch: WatchedFlight, sources: tuple[str, ...]) -> dict[tuple[str, str], Match]:
    """Nonstop trips on exactly `watch.flight` for `watch.depart_date`, keyed by (cabin, source).
    When several trip variants match the same key, keep the most seats then fewest miles."""
    want_date = watch.depart_date.isoformat()
    best: dict[tuple[str, str], Match] = {}
    for row in payload:
        source = str(row.get("Source") or "").lower()
        if source not in sources:
            continue
        if str(row.get("Date") or "")[:10] != want_date:
            continue
        for t in row.get("AvailabilityTrips") or []:
            cabin = str(t.get("Cabin") or "").lower()
            if cabin not in watch.cabins:
                continue
            flights = [normalize_flight(x) for x in str(t.get("FlightNumbers") or "").split(",") if x.strip()]
            if flights != [watch.flight]:
                continue
            if str(t.get("OriginAirport") or watch.origin).upper() != watch.origin:
                continue
            if str(t.get("DestinationAirport") or watch.destination).upper() != watch.destination:
                continue
            miles = int(t.get("MileageCost") or 0)
            if miles <= 0:
                continue
            m = Match(
                source=source,
                cabin=cabin,
                seats=int(t.get("RemainingSeats") or 0),
                miles=miles,
                fees_cents=seatsaero._taxes_to_cents(t.get("TotalTaxes")),
                availability_id=str(t.get("AvailabilityID") or row.get("ID") or "") or None,
                updated_at=t.get("UpdatedAt") or row.get("UpdatedAt"),
            )
            key = (cabin, source)
            cur = best.get(key)
            if cur is None or (m.seats, -m.miles) > (cur.seats, -cur.miles):
                best[key] = m
    return best


def run_check(
    conn: sqlite3.Connection,
    client: seatsaero.SeatsAeroClient,
    cfg: WatchConfig,
    *,
    today: date | None = None,
    now: str | None = None,
) -> list[Alert]:
    """Query every not-yet-departed watched flight, reconcile with stored state, and
    return the alerts to dispatch. `RateLimited` propagates; other per-flight errors
    are logged and that flight's state is left untouched."""
    today = today or datetime.now(timezone.utc).date()
    now = now or datetime.now(timezone.utc).strftime(ISO)
    alerts: list[Alert] = []
    for w in cfg.flights:
        if w.depart_date < today:
            log.info("watch %s %s: departed, skipping", w.flight, w.depart_date)
            continue
        sources = w.sources or cfg.sources
        try:
            payload = client.cached_search(
                origin=w.origin, destination=w.destination,
                start=w.depart_date, end=w.depart_date,
                sources=sources, include_trips=True,
            )
        except seatsaero.RateLimited:
            raise
        except Exception as e:  # network / 5xx: don't flip state to "gone" on a failed read
            log.warning("watch %s %s: search failed: %s", w.flight, w.depart_date, e)
            continue
        matches = find_matches(payload, w, sources)
        alert = reconcile(conn, w, sources, matches, cfg, now=now)
        if alert:
            alerts.append(alert)
    return alerts


def reconcile(
    conn: sqlite3.Connection,
    w: WatchedFlight,
    sources: tuple[str, ...],
    matches: dict[tuple[str, str], Match],
    cfg: WatchConfig,
    *,
    now: str,
) -> Alert | None:
    """Apply one check's matches to `flight_watch` state; return a combined alert for
    this flight if anything opened, grew, or (when notify_gone) disappeared."""
    d = w.depart_date.isoformat()
    prev = {
        (r["cabin"], r["source"]): r
        for r in conn.execute(
            "SELECT * FROM flight_watch WHERE flight = ? AND depart_date = ?", (w.flight, d)
        )
    }
    qualifying = {
        k: m for k, m in matches.items()
        if m.miles <= cfg.max_miles.get(m.source, 10**9)
    }
    for k, m in matches.items():
        if k not in qualifying:
            log.info(
                "watch %s %s: %s %s at %s mi is over the %s cap",
                w.flight, d, m.source, m.cabin, f"{m.miles:,}", f"{cfg.max_miles[m.source]:,}",
            )

    opened: list[Match] = []
    grew: list[tuple[Match, int]] = []
    gone: list[sqlite3.Row] = []

    with _tx(conn):
        for k, m in qualifying.items():
            p = prev.get(k)
            alert_now = False
            if p is None or not p["available"]:
                opened.append(m)
                alert_now = True
            elif m.seats > (p["last_alerted_seats"] or 0):
                grew.append((m, p["last_alerted_seats"] or 0))
                alert_now = True
            conn.execute(
                """
                INSERT INTO flight_watch (flight, depart_date, origin, destination, cabin, source,
                    available, seats, miles, fees_cents, availability_id, data_updated_at,
                    first_seen_at, last_seen_at, last_checked_at, last_alerted_at, last_alerted_seats)
                VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (flight, depart_date, cabin, source) DO UPDATE SET
                    available = 1,
                    seats = excluded.seats,
                    miles = excluded.miles,
                    fees_cents = excluded.fees_cents,
                    availability_id = COALESCE(excluded.availability_id, flight_watch.availability_id),
                    data_updated_at = excluded.data_updated_at,
                    first_seen_at = CASE WHEN flight_watch.available = 1
                                         THEN flight_watch.first_seen_at ELSE excluded.first_seen_at END,
                    last_seen_at = excluded.last_seen_at,
                    last_checked_at = excluded.last_checked_at,
                    last_alerted_at = COALESCE(excluded.last_alerted_at, flight_watch.last_alerted_at),
                    last_alerted_seats = COALESCE(excluded.last_alerted_seats, flight_watch.last_alerted_seats)
                """,
                (
                    w.flight, d, w.origin, w.destination, m.cabin, m.source,
                    m.seats, m.miles, m.fees_cents, m.availability_id, m.updated_at,
                    now, now, now,
                    now if alert_now else None,
                    m.seats if alert_now else None,
                ),
            )
        for k, p in prev.items():
            cabin, source = k
            if k in qualifying or cabin not in w.cabins or source not in sources:
                continue
            if p["available"]:
                gone.append(p)
            conn.execute(
                "UPDATE flight_watch SET available = 0, last_checked_at = ? "
                "WHERE flight = ? AND depart_date = ? AND cabin = ? AND source = ?",
                (now, w.flight, d, cabin, source),
            )

    for m in opened:
        log.warning("watch %s %s: %s %s OPEN (%s)", w.flight, d, m.source, m.cabin, _seats(m.seats))
    for m, before in grew:
        log.warning("watch %s %s: %s %s seats %d → %d", w.flight, d, m.source, m.cabin, before, m.seats)
    for p in gone:
        log.info("watch %s %s: %s %s no longer showing", w.flight, d, p["source"], p["cabin"])
    if not opened and not grew and not (gone and cfg.notify_gone):
        log.info("watch %s %s: no change (%d qualifying)", w.flight, d, len(qualifying))
        return None
    return render_alert(w, opened, grew, gone if cfg.notify_gone else [], cfg.priority)


def render_alert(
    w: WatchedFlight,
    opened: list[Match],
    grew: list[tuple[Match, int]],
    gone: list[sqlite3.Row],
    priority: int,
) -> Alert:
    when = f"{w.depart_date:%a %b} {w.depart_date.day}"
    route = f"{w.flight} {w.origin}→{w.destination} {when}"
    fresh = sorted(opened + [m for m, _ in grew], key=_sort_key)
    lines: list[str] = []
    if fresh:
        cabins = "/".join(dict.fromkeys(m.cabin for m in fresh))
        verb = "OPEN" if opened else "more seats"
        title = f"{route}: {cabins} {verb}"
        for m in opened:
            lines.append(f"NEW  {_program(m.source)} {m.cabin}: {_seats(m.seats)}, {_price(m)}")
        for m, before in grew:
            lines.append(f"MORE {_program(m.source)} {m.cabin}: {_seats(before)} → {_seats(m.seats)}, {_price(m)}")
        if any(m.source in AA_PARTNER_SPACE for m in fresh):
            lines.append("→ Book with AAdvantage miles on aa.com.")
            if not any(m.source == "american" for m in fresh):
                lines.append("  (Seen via a partner program; the same space usually shows on aa.com.)")
        if any(m.source == "jal" for m in fresh):
            lines.append("→ JAL space: book with JAL Mileage Bank miles on jal.co.jp (Citi TY → JAL first).")
        as_of = max((m.updated_at or "" for m in fresh), default="")
        if as_of:
            lines.append(f"seats.aero data as of {as_of[:16].replace('T', ' ')}Z")
        if w.note:
            lines.append(w.note)
        aa = any(m.source in AA_PARTNER_SPACE for m in fresh)
        primary = fresh[0]
        if aa:
            url, url_title = aa_award_url(w.origin, w.destination, w.depart_date), "Search aa.com"
        else:
            url, url_title = _seatsaero_link(w, primary), "Open seats.aero"
        lines.append(f"seats.aero: {_seatsaero_link(w, primary)}")
        prio = priority
    else:
        title = f"{route}: award space gone"
        url, url_title = None, None
        prio = -1
    for p in gone:
        lines.append(
            f"GONE {_program(p['source'])} {p['cabin']} (last {_seats(p['seats'])}, {p['miles']:,} mi)"
        )
    return Alert(pair_id=0, title=title, body="\n".join(lines), url=url, url_title=url_title, priority=prio)


def aa_award_url(origin: str, destination: str, depart: date, pax: int = 1) -> str:
    """aa.com one-way award search deep link (results page, all cabins)."""
    slices = json.dumps(
        [{"orig": origin, "origNearby": False, "dest": destination, "destNearby": False,
          "date": depart.isoformat()}],
        separators=(",", ":"),
    )
    return (
        "https://www.aa.com/booking/search?locale=en_US"
        f"&pax={pax}&adult={pax}&type=OneWay&searchType=Award&cabin=&carriers=ALL"
        f"&slices={quote(slices)}"
    )


def _seatsaero_link(w: WatchedFlight, m: Match) -> str:
    if m.availability_id:
        return seatsaero.availability_url(m.availability_id)
    return seatsaero.search_url(
        origin=w.origin, destination=w.destination, depart_date=w.depart_date, source=m.source,
    )


def _sort_key(m: Match) -> tuple:
    cabin_rank = CABIN_ORDER.index(m.cabin) if m.cabin in CABIN_ORDER else 99
    return (cabin_rank, m.source != "american", m.miles)


def _program(source: str) -> str:
    return PROGRAM_LABEL.get(source, source)


def _seats(n: int) -> str:
    if n <= 0:
        return "seats not reported"
    return f"{n} seat{'s' if n != 1 else ''}"


def _price(m: Match) -> str:
    return f"{m.miles:,} mi + ${m.fees_cents / 100:,.0f}"


class _tx:
    """BEGIN/COMMIT that tolerates being nested inside an open transaction."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.owns = False

    def __enter__(self):
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN")
            self.owns = True
        return self.conn

    def __exit__(self, exc_type, *_):
        if self.owns:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
