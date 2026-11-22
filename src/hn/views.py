"""Read-only contract calls: governor family, counting rule, tallies, quorum, state."""
from __future__ import annotations

from eth_abi import decode as abi_decode, encode as abi_encode

from .constants import FN, STATE, TOKEN_GETTERS, selector
from .rpc import Rpc, RpcError


def _call(rpc: Rpc, to: str, sel: str, args_types=(), args=(), block="latest") -> bytes | None:
    data = sel + (abi_encode(list(args_types), list(args)).hex() if args_types else "")
    try:
        out = rpc.eth_call(to, data, block)
    except RpcError:
        return None
    if not out or out == "0x":
        return None
    return bytes.fromhex(out[2:])


def voting_token(rpc: Rpc, gov: str, block="latest") -> tuple[str | None, str | None]:
    """(token address, getter used). Tries known getter names; None if unresolved."""
    for g in TOKEN_GETTERS:
        raw = _call(rpc, gov, selector(g), block=block)
        if raw and len(raw) >= 32:
            addr = "0x" + raw[12:32].hex()
            if int(addr, 16) != 0:
                return addr.lower(), g
    return None, None


def counting_mode(rpc: Rpc, gov: str, block="latest") -> str | None:
    raw = _call(rpc, gov, FN["COUNTING_MODE"], block=block)
    if not raw:
        return None
    try:
        return abi_decode(["string"], raw)[0]
    except Exception:
        return None


def family(rpc: Rpc, gov: str, block="latest") -> tuple[str, str | None]:
    """('oz', counting_mode) | ('bravo', None) | ('unknown', None)."""
    cm = counting_mode(rpc, gov, block)
    if cm is not None:
        return "oz", cm
    if _call(rpc, gov, FN["quorumVotes"], block=block) is not None:
        return "bravo", None
    return "unknown", None


def state(rpc: Rpc, gov: str, pid: int, block) -> str | None:
    raw = _call(rpc, gov, FN["state"], ["uint256"], [pid], block)
    return STATE.get(int.from_bytes(raw[:32], "big")) if raw else None


def tallies(rpc: Rpc, gov: str, fam: str, pid: int, block) -> dict | None:
    """{'for','against','abstain'} as ints, from the contract's own storage."""
    if fam == "oz":
        raw = _call(rpc, gov, FN["proposalVotes"], ["uint256"], [pid], block)
        if raw and len(raw) >= 96:
            a, f, ab = abi_decode(["uint256", "uint256", "uint256"], raw[:96])
            return {"for": f, "against": a, "abstain": ab}
        # GovernorCompatibilityBravo keeps tallies in the Bravo-style proposals() struct
    raw = _call(rpc, gov, FN["proposals"], ["uint256"], [pid], block)
    if not raw:
        return None
    words = [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]
    # Bravo: id, proposer, eta, startBlock, endBlock, forVotes, againstVotes, abstainVotes, canceled, executed
    # Alpha: id, proposer, eta, startBlock, endBlock, forVotes, againstVotes, canceled, executed
    if len(words) >= 10:
        return {"for": words[5], "against": words[6], "abstain": words[7]}
    if len(words) == 9:
        return {"for": words[5], "against": words[6], "abstain": 0}
    return None


def quorum(rpc: Rpc, gov: str, fam: str, snapshot: int | None, block) -> int | None:
    """Quorum as the contract reports it. OZ: quorum(snapshot); Bravo: quorumVotes() at `block`."""
    if fam == "oz" and snapshot is not None:
        raw = _call(rpc, gov, FN["quorum"], ["uint256"], [snapshot], block)
    else:
        raw = _call(rpc, gov, FN["quorumVotes"], block=block)
    return int.from_bytes(raw[:32], "big") if raw else None


def proposal_snapshot(rpc: Rpc, gov: str, pid: int, block) -> int | None:
    raw = _call(rpc, gov, FN["proposalSnapshot"], ["uint256"], [pid], block)
    return int.from_bytes(raw[:32], "big") if raw else None
