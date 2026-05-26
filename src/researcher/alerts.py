from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .config import Env
from .seatsaero import availability_url, search_url

log = logging.getLogger(__name__)
ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Alert:
    pair_id: int
    title: str
    body: str
    url: str | None = None


@dataclass(frozen=True)
class AlertBatch:
    to_send: list[Alert]
    suppressed_pair_ids: list[int]

    @property
    def all_pair_ids(self) -> list[int]:
        return [a.pair_id for a in self.to_send] + self.suppressed_pair_ids


_HUGE = 10**9  # treat NULL duration as infinity for ordering


def build_alerts_for_new_viable(conn: sqlite3.Connection) -> AlertBatch:
    """Find pairs newly transitioned to 'viable', then prune noise via:
      1. per-outbound-flight dedup (cheapest+shortest pair wins per (out_origin, out_dest, out_date))
      2. Pareto frontier on (total_miles, total_duration_min) over the deduped set
    Returns the winners to send plus the pair_ids of suppressed (still seen + still viable
    but redundant) pairs so the caller can mark them 'alerted' and stop re-queueing them."""
    rows = conn.execute(
        """
        SELECT p.id AS pair_id, p.nights, p.total_miles, p.total_fees_cents, p.bookable_from,
               ol.source AS out_src, ol.origin AS out_org, ol.destination AS out_dst,
               ol.depart_date AS out_date, ol.cabin AS out_cabin,
               ol.seats_remaining AS out_seats, ol.miles AS out_miles, ol.fees_cents AS out_fees,
               ol.last_snapshot_json AS out_snap, ol.availability_id AS out_avail,
               ol.duration_min AS out_dur, ol.stops AS out_stops, ol.carriers AS out_carriers,
               rl.source AS ret_src, rl.origin AS ret_org, rl.destination AS ret_dst,
               rl.depart_date AS ret_date, rl.cabin AS ret_cabin,
               rl.seats_remaining AS ret_seats, rl.miles AS ret_miles, rl.fees_cents AS ret_fees,
               rl.last_snapshot_json AS ret_snap, rl.availability_id AS ret_avail,
               rl.duration_min AS ret_dur, rl.stops AS ret_stops, rl.carriers AS ret_carriers
          FROM pairs p
          JOIN legs ol ON ol.id = p.out_leg_id
          JOIN legs rl ON rl.id = p.ret_leg_id
         WHERE p.state = 'viable'
           AND (p.last_alerted_at IS NULL OR p.last_alerted_at < p.last_seen_at)
        """
    ).fetchall()

    if not rows:
        return AlertBatch(to_send=[], suppressed_pair_ids=[])

    candidates = [dict(r) for r in rows]
    for c in candidates:
        c["total_duration"] = (c["out_dur"] or _HUGE) + (c["ret_dur"] or _HUGE)

    # 1) Per-outbound-flight dedup: one pair per (out_origin, out_dest, out_date).
    by_route: dict[tuple, dict] = {}
    for c in candidates:
        key = (c["out_org"], c["out_dst"], c["out_date"])
        winner = by_route.get(key)
        if winner is None or (c["total_miles"], c["total_duration"]) < (winner["total_miles"], winner["total_duration"]):
            by_route[key] = c
    deduped = list(by_route.values())

    # 2) Pareto frontier on (total_miles, total_duration) — drop pairs dominated by another.
    frontier: list[dict] = []
    for c in deduped:
        dominated = any(
            other["total_miles"] <= c["total_miles"]
            and other["total_duration"] <= c["total_duration"]
            and (other["total_miles"] < c["total_miles"] or other["total_duration"] < c["total_duration"])
            for other in deduped if other is not c
        )
        if not dominated:
            frontier.append(c)

    winner_ids = {c["pair_id"] for c in frontier}
    suppressed_ids = [c["pair_id"] for c in candidates if c["pair_id"] not in winner_ids]

    to_send = [_render_alert(c) for c in sorted(frontier, key=lambda x: (x["total_miles"], x["total_duration"]))]
    log.info(
        "alert batch: %d candidate viable → %d after per-route dedup → %d on Pareto frontier (%d suppressed)",
        len(candidates), len(deduped), len(frontier), len(suppressed_ids),
    )
    return AlertBatch(to_send=to_send, suppressed_pair_ids=suppressed_ids)


def _render_alert(r: dict) -> Alert:
    cabin_tag = r["out_cabin"] if r["out_cabin"] == r["ret_cabin"] else f"{r['out_cabin']}/{r['ret_cabin']}"
    title = (
        f"{r['out_org']}→{r['out_dst']} {r['out_date']} / "
        f"{r['ret_org']}→{r['ret_dst']} {r['ret_date']} "
        f"[{cabin_tag}]"
    )
    out_desc = _describe_leg(
        r["out_cabin"], r["out_seats"], r["out_miles"], r["out_fees"], r["out_snap"],
        r["out_dur"], r["out_stops"], r["out_carriers"],
    )
    ret_desc = _describe_leg(
        r["ret_cabin"], r["ret_seats"], r["ret_miles"], r["ret_fees"], r["ret_snap"],
        r["ret_dur"], r["ret_stops"], r["ret_carriers"],
    )
    out_url = _leg_url(r["out_avail"], r["out_cabin"], r["out_src"], r["out_org"], r["out_dst"], r["out_date"])
    ret_url = _leg_url(r["ret_avail"], r["ret_cabin"], r["ret_src"], r["ret_org"], r["ret_dst"], r["ret_date"])
    body = (
        f"{r['nights']} nights | pool: {r['bookable_from']}\n"
        f"OUT: {r['out_src']} {out_desc}\n  {out_url}\n"
        f"RET: {r['ret_src']} {ret_desc}\n  {ret_url}\n"
        f"TOTAL: {r['total_miles']:,}mi + ${r['total_fees_cents']/100:.0f}"
    )
    return Alert(pair_id=int(r["pair_id"]), title=title, body=body, url=out_url)


def _leg_url(availability_id: str | None, cabin: str, source: str, origin: str, dest: str, depart_iso: str) -> str:
    from datetime import date
    if availability_id:
        return availability_url(availability_id)
    # Mixed/synthetic legs have no availability_id — fall back to the search page.
    src = source.split("+")[0] if cabin == "mixed" else source
    return search_url(origin=origin, destination=dest, depart_date=date.fromisoformat(depart_iso), source=src)


def _describe_leg(
    cabin: str, seats: int, miles: int, fees_cents: int, snap_json: str | None,
    duration_min: int | None = None, stops: int | None = None, carriers: str | None = None,
) -> str:
    """One-line per-leg description; expands the pax-split breakdown for synthetic 'mixed' legs."""
    if cabin == "mixed" and snap_json:
        try:
            snap = json.loads(snap_json)
            parts = [
                f"{a['count']}x{a['cabin'][:1].upper()}@{a['miles_per_pax']:,}mi"
                for a in snap.get("allocations", [])
            ]
            return f"[mixed-split] {' + '.join(parts)} = {snap.get('total_miles', 0):,}mi total"
        except (ValueError, KeyError):
            pass
    base = f"[{cabin}] {seats} seats, {miles:,}mi + ${fees_cents/100:.0f}/pax"
    extras = []
    if duration_min:
        extras.append(f"{duration_min // 60}h{duration_min % 60:02d}m")
    if stops is not None:
        extras.append("nonstop" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}")
    if carriers:
        extras.append(carriers)
    return base + (" | " + " · ".join(extras) if extras else "")


def dispatch(env: Env, alerts: list[Alert]) -> None:
    if not alerts:
        return
    for a in alerts:
        if env.ntfy_topic:
            _send_ntfy(env, a)
        if env.pushover_token and env.pushover_user:
            _send_pushover(env, a)


def mark_alerted(conn: sqlite3.Connection, pair_ids: list[int]) -> None:
    if not pair_ids:
        return
    now = datetime.now(timezone.utc).strftime(ISO)
    placeholders = ",".join("?" * len(pair_ids))
    conn.execute(
        f"UPDATE pairs SET last_alerted_at = ?, state = 'alerted' WHERE id IN ({placeholders})",
        (now, *pair_ids),
    )


def _send_ntfy(env: Env, alert: Alert) -> None:
    url = f"{env.ntfy_server.rstrip('/')}/{env.ntfy_topic}"
    try:
        r = httpx.post(
            url,
            data=alert.body.encode("utf-8"),
            headers={"Title": alert.title, "Priority": "high", "Tags": "airplane,fire"},
            timeout=10,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("ntfy send failed for pair %s: %s", alert.pair_id, e)


def _send_pushover(env: Env, alert: Alert) -> None:
    try:
        r = httpx.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": env.pushover_token,
                "user": env.pushover_user,
                "title": alert.title,
                "message": alert.body,
                "priority": 1,
            },
            timeout=10,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("pushover send failed for pair %s: %s", alert.pair_id, e)
