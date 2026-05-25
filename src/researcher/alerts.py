from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .config import Env

log = logging.getLogger(__name__)
ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Alert:
    pair_id: int
    title: str
    body: str
    url: str | None = None


def build_alerts_for_new_viable(conn: sqlite3.Connection) -> list[Alert]:
    """Find pairs that transitioned into 'viable' since last alert and haven't been alerted yet."""
    rows = conn.execute(
        """
        SELECT p.id AS pair_id, p.nights, p.total_miles, p.total_fees_cents, p.bookable_from,
               ol.source AS out_src, ol.origin AS out_org, ol.destination AS out_dst,
               ol.depart_date AS out_date, ol.cabin AS out_cabin,
               ol.seats_remaining AS out_seats, ol.miles AS out_miles, ol.fees_cents AS out_fees,
               rl.source AS ret_src, rl.origin AS ret_org, rl.destination AS ret_dst,
               rl.depart_date AS ret_date, rl.cabin AS ret_cabin,
               rl.seats_remaining AS ret_seats, rl.miles AS ret_miles, rl.fees_cents AS ret_fees
          FROM pairs p
          JOIN legs ol ON ol.id = p.out_leg_id
          JOIN legs rl ON rl.id = p.ret_leg_id
         WHERE p.state = 'viable'
           AND (p.last_alerted_at IS NULL OR p.last_alerted_at < p.last_seen_at)
        """
    ).fetchall()

    alerts: list[Alert] = []
    for r in rows:
        cabin_tag = r["out_cabin"] if r["out_cabin"] == r["ret_cabin"] else f"{r['out_cabin']}/{r['ret_cabin']}"
        title = (
            f"{r['out_org']}→{r['out_dst']} {r['out_date']} / "
            f"{r['ret_org']}→{r['ret_dst']} {r['ret_date']} "
            f"[{cabin_tag}]"
        )
        body = (
            f"{r['nights']} nights | pool: {r['bookable_from']}\n"
            f"OUT [{r['out_cabin']}]: {r['out_src']} {r['out_seats']} seats, "
            f"{r['out_miles']:,}mi + ${r['out_fees']/100:.0f}/pax\n"
            f"RET [{r['ret_cabin']}]: {r['ret_src']} {r['ret_seats']} seats, "
            f"{r['ret_miles']:,}mi + ${r['ret_fees']/100:.0f}/pax\n"
            f"TOTAL (4 pax): {r['total_miles']:,}mi + ${r['total_fees_cents']/100:.0f}"
        )
        alerts.append(Alert(pair_id=int(r["pair_id"]), title=title, body=body))
    return alerts


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
