from __future__ import annotations

from dataclasses import dataclass

from .config import BalancesConfig


@dataclass(frozen=True)
class Feasibility:
    bookable: bool
    pool_name: str | None
    reason: str


def assess(
    *,
    program: str,
    miles_per_pax: int,
    fees_cents_per_pax: int,
    passengers: int,
    cabin: str,
    balances: BalancesConfig,
) -> Feasibility:
    """Decide whether a single program can fund N pax on a leg."""
    fees_usd_per_pax = fees_cents_per_pax / 100
    if fees_usd_per_pax > balances.max_fees_per_pax_usd:
        return Feasibility(False, None, f"fees ${fees_usd_per_pax:.0f}/pax exceeds cap")

    cap = balances.max_miles_per_pax.get(cabin)
    if cap and miles_per_pax > cap:
        return Feasibility(False, None, f"miles {miles_per_pax}/pax exceeds {cabin} cap {cap}")

    total_needed = miles_per_pax * passengers
    direct = balances.direct.get(program, 0)
    transfer = sum(p.balance for p in balances.transferable if program in p.targets)
    effective = direct + transfer
    if effective < total_needed:
        return Feasibility(
            False,
            None,
            f"{program}: need {total_needed:,}, have {effective:,} (direct {direct:,} + transfer {transfer:,})",
        )

    if direct >= total_needed:
        return Feasibility(True, program, f"direct {program} balance covers")

    contributing = [p.name for p in balances.transferable if program in p.targets and p.balance > 0]
    return Feasibility(True, program, f"{program} + transfer from {','.join(contributing) or 'pool'}")


def pair_feasibility(
    *,
    out_program: str,
    ret_program: str,
    out_miles: int,
    ret_miles: int,
    out_fees_cents: int,
    ret_fees_cents: int,
    passengers: int,
    cabin: str,
    balances: BalancesConfig,
) -> Feasibility:
    """Bookability of a pair. If same program, check combined draw; else check each leg independently."""
    if out_program == ret_program:
        return assess(
            program=out_program,
            miles_per_pax=out_miles + ret_miles,
            fees_cents_per_pax=out_fees_cents + ret_fees_cents,
            passengers=passengers,
            cabin=cabin,
            balances=balances,
        )

    out_f = assess(
        program=out_program,
        miles_per_pax=out_miles,
        fees_cents_per_pax=out_fees_cents,
        passengers=passengers,
        cabin=cabin,
        balances=balances,
    )
    if not out_f.bookable:
        return Feasibility(False, None, f"outbound infeasible: {out_f.reason}")
    ret_f = assess(
        program=ret_program,
        miles_per_pax=ret_miles,
        fees_cents_per_pax=ret_fees_cents,
        passengers=passengers,
        cabin=cabin,
        balances=balances,
    )
    if not ret_f.bookable:
        return Feasibility(False, None, f"return infeasible: {ret_f.reason}")
    return Feasibility(True, f"{out_program}+{ret_program}", f"split: {out_f.reason}; {ret_f.reason}")
