from __future__ import annotations

from dataclasses import dataclass

from .config import BalancesConfig


@dataclass(frozen=True)
class Feasibility:
    bookable: bool
    pool_name: str | None
    reason: str


def _check_caps(
    *,
    miles_per_pax: int,
    fees_cents_per_pax: int,
    cabin: str,
    balances: BalancesConfig,
) -> Feasibility | None:
    """Per-leg cost ceilings. Returns Feasibility(False, ...) on failure, else None."""
    fees_usd_per_pax = fees_cents_per_pax / 100
    if fees_usd_per_pax > balances.max_fees_per_pax_usd:
        return Feasibility(False, None, f"fees ${fees_usd_per_pax:.0f}/pax exceeds cap")
    cap = balances.max_miles_per_pax.get(cabin)
    if cap and miles_per_pax > cap:
        return Feasibility(False, None, f"miles {miles_per_pax}/pax exceeds {cabin} cap {cap}")
    return None


def _check_balance(
    *,
    program: str,
    miles_per_pax: int,
    passengers: int,
    balances: BalancesConfig,
) -> Feasibility:
    """Does the program (direct + transferable pools) have enough miles?"""
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


def assess(
    *,
    program: str,
    miles_per_pax: int,
    fees_cents_per_pax: int,
    passengers: int,
    cabin: str,
    balances: BalancesConfig,
) -> Feasibility:
    """Decide whether a single program can fund N pax on a leg in one cabin."""
    cap_fail = _check_caps(
        miles_per_pax=miles_per_pax,
        fees_cents_per_pax=fees_cents_per_pax,
        cabin=cabin,
        balances=balances,
    )
    if cap_fail:
        return cap_fail
    return _check_balance(
        program=program, miles_per_pax=miles_per_pax, passengers=passengers, balances=balances
    )


def pair_feasibility(
    *,
    out_program: str,
    ret_program: str,
    out_miles: int,
    ret_miles: int,
    out_fees_cents: int,
    ret_fees_cents: int,
    passengers: int,
    out_cabin: str,
    ret_cabin: str,
    balances: BalancesConfig,
) -> Feasibility:
    """Bookability of a pair, supporting mixed cabins across the two legs.
    Cost ceilings apply per-leg against each leg's own cabin cap. Balance funding
    aggregates across the pair when both legs share a program; otherwise each
    leg must be funded independently (two-ticket split)."""
    out_cap_fail = _check_caps(
        miles_per_pax=out_miles, fees_cents_per_pax=out_fees_cents,
        cabin=out_cabin, balances=balances,
    )
    if out_cap_fail:
        return Feasibility(False, None, f"outbound: {out_cap_fail.reason}")
    ret_cap_fail = _check_caps(
        miles_per_pax=ret_miles, fees_cents_per_pax=ret_fees_cents,
        cabin=ret_cabin, balances=balances,
    )
    if ret_cap_fail:
        return Feasibility(False, None, f"return: {ret_cap_fail.reason}")

    if out_program == ret_program:
        return _check_balance(
            program=out_program, miles_per_pax=out_miles + ret_miles,
            passengers=passengers, balances=balances,
        )

    out_b = _check_balance(
        program=out_program, miles_per_pax=out_miles,
        passengers=passengers, balances=balances,
    )
    if not out_b.bookable:
        return Feasibility(False, None, f"outbound infeasible: {out_b.reason}")
    ret_b = _check_balance(
        program=ret_program, miles_per_pax=ret_miles,
        passengers=passengers, balances=balances,
    )
    if not ret_b.bookable:
        return Feasibility(False, None, f"return infeasible: {ret_b.reason}")
    return Feasibility(True, f"{out_program}+{ret_program}", f"split: {out_b.reason}; {ret_b.reason}")
