"""Generic vote-stage outcome functions (Tier A). Pure functions.

These reproduce the *vote-stage* result only (Succeeded vs Defeated). Post-vote
states (Queued, Executed, Expired) all count as "passed the vote".
"""
from __future__ import annotations

from urllib.parse import parse_qs


def quorum_basis(fam: str, counting_mode: str | None) -> tuple[str, ...] | None:
    """Which tallies count towards quorum. None = rule not understood -> Tier B."""
    if fam == "bravo":
        return ("for",)
    if fam == "oz" and counting_mode:
        q = parse_qs(counting_mode).get("quorum", [""])[0]
        parts = tuple(p for p in q.split(",") if p)
        support = parse_qs(counting_mode).get("support", [""])[0]
        if support != "bravo" or not parts or not set(parts) <= {"for", "against", "abstain"}:
            return None
        return parts
    return None


def passes(t: dict, q: int, basis: tuple[str, ...], fam: str) -> bool:
    counted = sum(t[k] for k in basis)
    if fam == "bravo":
        return t["for"] > t["against"] and t["for"] >= q
    # OZ GovernorCountingSimple: _quorumReached uses >=, _voteSucceeded uses for > against
    return counted >= q and t["for"] > t["against"]


def flip_mechanism(t: dict, q: int, basis: tuple[str, ...]) -> str:
    counted = sum(t[k] for k in basis)
    fails_q = counted < q
    fails_m = not (t["for"] > t["against"])
    return {(True, True): "both", (True, False): "quorum", (False, True): "majority"}.get((fails_q, fails_m), "none")
