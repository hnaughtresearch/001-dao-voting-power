"""Decode raw governor logs into flat rows. Pure functions, no network."""
from __future__ import annotations

from eth_abi import decode as abi_decode

from .constants import TOPIC_TO_NAME


def _b(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr[2:] if hexstr.startswith("0x") else hexstr)


def _addr_from_topic(t: str) -> str:
    return "0x" + t[-40:].lower()


def _safe_decode(types: list[str], data: bytes) -> list:
    """Decode; on invalid UTF-8 in strings, decode strings as bytes and repair."""
    try:
        return list(abi_decode(types, data))
    except Exception:
        alt = [t.replace("string", "bytes") for t in types]
        out = list(abi_decode(alt, data))
        for i, t in enumerate(types):
            if t == "string":
                out[i] = out[i].decode("utf-8", "replace")
            elif t == "string[]":
                out[i] = [x.decode("utf-8", "replace") for x in out[i]]
        return out


def decode_log(log: dict) -> dict | None:
    """Return a normalised event row, or None for unknown topics."""
    topics = log["topics"]
    name = TOPIC_TO_NAME.get(topics[0].lower()) if topics else None
    if name is None:
        return None
    data = _b(log["data"])
    base = {
        "event": name,
        "governor": log["address"].lower(),
        "block": int(log["blockNumber"], 16),
        "tx": log["transactionHash"],
        "log_index": int(log["logIndex"], 16),
    }
    if name == "ProposalCreated":
        v = _safe_decode(["uint256", "address", "address[]", "uint256[]", "string[]", "bytes[]",
                          "uint256", "uint256", "string"], data)
        base.update(proposal_id=str(v[0]), proposer=v[1].lower(), n_actions=len(v[2]),
                    start_block=v[6], end_block=v[7], description=v[8])
    elif name in ("VoteCast", "VoteCastWithParams"):
        types = ["uint256", "uint8", "uint256", "string"] + (["bytes"] if name == "VoteCastWithParams" else [])
        v = _safe_decode(types, data)
        base.update(voter=_addr_from_topic(topics[1]), proposal_id=str(v[0]), support=int(v[1]),
                    weight=str(v[2]), reason=v[3],
                    has_params=(name == "VoteCastWithParams" and len(v[4]) > 0))
    elif name == "VoteCastAlpha":
        v = abi_decode(["address", "uint256", "bool", "uint256"], data)
        base.update(voter=v[0].lower(), proposal_id=str(v[1]), support=1 if v[2] else 0,
                    weight=str(v[3]), reason="", has_params=False)
    elif name == "ProposalQueued":
        v = abi_decode(["uint256", "uint256"], data)
        base.update(proposal_id=str(v[0]), eta=str(v[1]))
    else:  # ProposalExecuted / ProposalCanceled
        v = abi_decode(["uint256"], data)
        base.update(proposal_id=str(v[0]))
    return base
