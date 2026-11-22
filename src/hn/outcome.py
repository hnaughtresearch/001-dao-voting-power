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
        qs = parse_qs(counting_mode)
        q = qs.get("quorum", [""])[0]
        support = qs.get("support", [""])[0]
        if support not in ("bravo", "bravo,fractional") or "counting" in qs or "success" in qs:
            return None
        if q == "bravo":                      # GovernorCompatibilityBravo: quorum counts For only
            return ("for",)
        parts = tuple(p for p in q.split(",") if p)
        if not parts or not set(parts) <= {"for", "against", "abstain"}:
            return None
        return parts
    return None


def is_election(counting_mode: str | None) -> bool:
    """Candidate elections (e.g. counting=n-winners / counting=threshold) have no pass/fail outcome."""
    return bool(counting_mode) and "counting" in parse_qs(counting_mode)


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
