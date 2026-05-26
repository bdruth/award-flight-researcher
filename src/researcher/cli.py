from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from . import alerts as alerts_mod
from . import db as db_mod
from . import pairs as pairs_mod
from . import seatsaero
from .config import BalancesConfig, Env, SearchConfig, load_balances, load_env, load_search

log = logging.getLogger("researcher")
ISO = "%Y-%m-%dT%H:%M:%SZ"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _load_all(search_path: Path, balances_path: Path) -> tuple[Env, SearchConfig, BalancesConfig]:
    env = load_env()
    _setup_logging(env.log_level)
    search = load_search(search_path)
    balances = load_balances(balances_path)
    return env, search, balances


def _poll_once(
    conn: sqlite3.Connection,
    env: Env,
    search: SearchConfig,
    balances: BalancesConfig,
) -> None:
    pairs_directional = []
    for o in search.routing.origins:
        for d in search.routing.destinations:
            pairs_directional.append((o, d))
            pairs_directional.append((d, o))

    with seatsaero.SeatsAeroClient(env.seatsaero_api_key) as client:
        for origin, dest in pairs_directional:
            started = datetime.now(timezone.utc).strftime(ISO)
            try:
                payload = client.cached_search(
                    origin=origin,
                    destination=dest,
                    start=search.trip.window_start,
                    end=search.trip.window_end,
                    sources=search.sources,
                )
            except Exception as e:
                log.warning("poll %s→%s failed: %s", origin, dest, e)
                conn.execute(
                    "INSERT INTO poll_log (source, origin, destination, started_at, finished_at, error) VALUES (?,?,?,?,?,?)",
                    ("*", origin, dest, started, datetime.now(timezone.utc).strftime(ISO), str(e)),
                )
                continue
            count = 0
            with db_mod.transaction(conn):
                for leg in seatsaero.normalize(
                    payload, cabins=search.cabins, min_seats=search.leg_filters.min_seats
                ):
                    if leg.source not in search.sources:
                        continue
                    pairs_mod.upsert_leg(conn, leg)
                    count += 1
            conn.execute(
                "INSERT INTO poll_log (source, origin, destination, started_at, finished_at, result_count) VALUES (?,?,?,?,?,?)",
                ("*", origin, dest, started, datetime.now(timezone.utc).strftime(ISO), count),
            )
            log.info("polled %s→%s: %d legs ingested", origin, dest, count)

        with db_mod.transaction(conn):
            fetched = pairs_mod.enrich_layovers(
                conn, client,
                layover_min=search.routing.layover_min,
                layover_max=search.routing.layover_max,
            )
        log.info("enrichment: fetched %d trip(s) from /trips", fetched)

    with db_mod.transaction(conn):
        synth = pairs_mod.synthesize_pax_splits(
            conn, pax=search.trip.passengers, cabins=search.cabins, balances=balances,
        )
        stats = pairs_mod.join_pairs(conn, search, balances)
    log.info(
        "synthesized %d pax-split leg(s); join: seen=%d new=%d promoted_viable=%d invalidated=%d",
        synth, stats.pairs_seen, stats.pairs_new, stats.pairs_promoted_viable, stats.pairs_invalidated,
    )

    new_alerts = alerts_mod.build_alerts_for_new_viable(conn)
    if new_alerts:
        alerts_mod.dispatch(env, new_alerts)
        with db_mod.transaction(conn):
            alerts_mod.mark_alerted(conn, [a.pair_id for a in new_alerts])
        log.info("dispatched %d alert(s)", len(new_alerts))


@click.group()
def cli() -> None:
    """award-flight-researcher: pair-centric award availability watcher."""


@cli.command("init-db")
@click.option("--search", "search_path", type=click.Path(path_type=Path), default="config/search.yaml")
@click.option("--balances", "balances_path", type=click.Path(path_type=Path), default="config/balances.yaml")
def init_db(search_path: Path, balances_path: Path) -> None:
    env, _, _ = _load_all(search_path, balances_path)
    conn = db_mod.connect(env.db_path)
    db_mod.init(conn)
    click.echo(f"initialized {env.db_path}")


@cli.command("run-once")
@click.option("--search", "search_path", type=click.Path(path_type=Path), default="config/search.yaml")
@click.option("--balances", "balances_path", type=click.Path(path_type=Path), default="config/balances.yaml")
def run_once(search_path: Path, balances_path: Path) -> None:
    env, search, balances = _load_all(search_path, balances_path)
    conn = db_mod.connect(env.db_path)
    db_mod.init(conn)
    _poll_once(conn, env, search, balances)


@cli.command("watch")
@click.option("--interval-minutes", default=15, show_default=True)
@click.option("--search", "search_path", type=click.Path(path_type=Path), default="config/search.yaml")
@click.option("--balances", "balances_path", type=click.Path(path_type=Path), default="config/balances.yaml")
def watch(interval_minutes: int, search_path: Path, balances_path: Path) -> None:
    env, search, balances = _load_all(search_path, balances_path)
    conn = db_mod.connect(env.db_path)
    db_mod.init(conn)
    while True:
        try:
            _poll_once(conn, env, search, balances)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("poll cycle failed")
        time.sleep(interval_minutes * 60)


@cli.command("pairs")
@click.option("--state", default="viable", type=click.Choice(["candidate", "viable", "alerted", "invalidated"]))
@click.option("--limit", default=50, show_default=True)
@click.option("--search", "search_path", type=click.Path(path_type=Path), default="config/search.yaml")
@click.option("--balances", "balances_path", type=click.Path(path_type=Path), default="config/balances.yaml")
def list_pairs(state: str, limit: int, search_path: Path, balances_path: Path) -> None:
    env, _, _ = _load_all(search_path, balances_path)
    conn = db_mod.connect(env.db_path)
    db_mod.init(conn)
    rows = conn.execute(
        """
        SELECT p.id, p.nights, p.total_miles, p.total_fees_cents, p.bookable_from, p.state,
               ol.source AS out_src, ol.origin AS out_org, ol.destination AS out_dst,
               ol.depart_date AS out_date, ol.cabin AS out_cabin, ol.seats_remaining AS out_seats,
               ol.availability_id AS out_avail,
               rl.source AS ret_src, rl.origin AS ret_org, rl.destination AS ret_dst,
               rl.depart_date AS ret_date, rl.cabin AS ret_cabin, rl.seats_remaining AS ret_seats,
               rl.availability_id AS ret_avail
          FROM pairs p
          JOIN legs ol ON ol.id = p.out_leg_id
          JOIN legs rl ON rl.id = p.ret_leg_id
         WHERE p.state = ?
         ORDER BY p.total_miles ASC, p.total_fees_cents ASC
         LIMIT ?
        """,
        (state, limit),
    ).fetchall()
    if not rows:
        click.echo(f"(no pairs in state '{state}')")
        return
    from datetime import date
    for r in rows:
        cabin_tag = r["out_cabin"] if r["out_cabin"] == r["ret_cabin"] else f"{r['out_cabin']}/{r['ret_cabin']}"
        out_seats_str = f"split×{r['out_seats']}" if r["out_cabin"] == "mixed" else f"{r['out_seats']} seats"
        ret_seats_str = f"split×{r['ret_seats']}" if r["ret_cabin"] == "mixed" else f"{r['ret_seats']} seats"
        out_url = (
            seatsaero.availability_url(r["out_avail"]) if r["out_avail"]
            else seatsaero.search_url(
                origin=r["out_org"], destination=r["out_dst"],
                depart_date=date.fromisoformat(r["out_date"]),
                source=r["out_src"].split("+")[0],
            )
        )
        ret_url = (
            seatsaero.availability_url(r["ret_avail"]) if r["ret_avail"]
            else seatsaero.search_url(
                origin=r["ret_org"], destination=r["ret_dst"],
                depart_date=date.fromisoformat(r["ret_date"]),
                source=r["ret_src"].split("+")[0],
            )
        )
        click.echo(
            f"#{r['id']:<5} [{cabin_tag:<17}] "
            f"{r['out_org']}->{r['out_dst']} {r['out_date']} ({r['out_src']}, {out_seats_str}) | "
            f"{r['ret_org']}->{r['ret_dst']} {r['ret_date']} ({r['ret_src']}, {ret_seats_str}) | "
            f"{r['nights']}n | {r['total_miles']:,}mi + ${r['total_fees_cents']/100:.0f} "
            f"via {r['bookable_from']}\n"
            f"          OUT: {out_url}\n"
            f"          RET: {ret_url}"
        )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
