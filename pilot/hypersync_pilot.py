"""Pilot: is Envio HyperSync a complete, usable source of governor logs? Free, no card.

Ground truth is the contracts themselves: Bravo and OZ governors store final vote tallies
on-chain, readable at the latest block with any ordinary (non-archive) RPC. If the vote
weights summed from HyperSync logs equal the stored tallies for every proposal, no vote is
missing or duplicated.

Checks
  1  connection: chain id and height for Ethereum, Arbitrum One, OP Mainnet
  2  history depth: Compound Governor Alpha (2020) logs are present from the start
  3  completeness, Ethereum: Uniswap Governor Bravo, every proposal, sum(votes) == tallies
  4  completeness, Arbitrum: Core Governor, every proposal, same test (OZ proposalVotes)
  5  completeness, Optimism: Agora governor, same test (expected to differ: custom modules)
  6  throughput: topic-only scan of ProposalCreated over the full history of each chain,
     i.e. a real piece of the frame, with timing and the rate limit actually observed

Run:
  $env:HYPERSYNC_TOKEN = "..."            # free token from envio.dev, no card
  py pilot\\hypersync_pilot.py
Optional overrides: ETH_RPC_URL, ARB_RPC_URL, OP_RPC_URL (defaults: public endpoints).
Writes pilot\\pilot_report.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import hypersync  # noqa: E402
from hypersync import ClientConfig, FieldSelection, LogField, LogSelection, Query  # noqa: E402

from hn.constants import TOPIC, AGAINST, FOR, ABSTAIN  # noqa: E402
from hn.decode import decode_log  # noqa: E402
from hn.rpc import Rpc  # noqa: E402
from hn import views  # noqa: E402

CHAINS = {
    "ethereum": {"hs": "https://eth.hypersync.xyz", "chain_id": 1,
                 "rpc": os.environ.get("ETH_RPC_URL", "https://ethereum-rpc.publicnode.com")},
    "arbitrum": {"hs": "https://arbitrum.hypersync.xyz", "chain_id": 42161,
                 "rpc": os.environ.get("ARB_RPC_URL", "https://arb1.arbitrum.io/rpc")},
    "optimism": {"hs": "https://optimism.hypersync.xyz", "chain_id": 10,
                 "rpc": os.environ.get("OP_RPC_URL", "https://mainnet.optimism.io")},
}
COMPOUND_ALPHA = "0xc0da01a04c3f3e0be433606045bb7017a7323e38"
UNISWAP_BRAVO = "0x408ed6354d4973f66138c91495f2f2fcbd8724c3"
ARB_CORE = "0xf07ded9dc292157749b6fd268e37df6ea38395b9"
OP_GOVERNOR = "0xcdf27f107725988f2261ce2256bdfcde8b382b10"
ALL_FIELDS = [LogField.ADDRESS, LogField.DATA, LogField.TOPIC0, LogField.TOPIC1, LogField.TOPIC2,
              LogField.TOPIC3, LogField.BLOCK_NUMBER, LogField.TRANSACTION_HASH, LogField.LOG_INDEX]
SUPPORT_KEY = {AGAINST: "against", FOR: "for", ABSTAIN: "abstain"}


def client(chain: str, token: str) -> hypersync.HypersyncClient:
    return hypersync.HypersyncClient(ClientConfig(url=CHAINS[chain]["hs"], api_token=token,
                                                  max_num_retries=8, proactive_rate_limit_sleep=True))


def to_rpc_shape(lg) -> dict:
    """HyperSync Log -> the eth_getLogs dict shape that hn.decode expects."""
    topics = [t for t in (lg.topics or []) if t]
    return {"address": lg.address, "topics": topics, "data": lg.data or "0x",
            "blockNumber": hex(lg.block_number), "transactionHash": lg.transaction_hash,
            "logIndex": hex(lg.log_index)}


async def fetch_logs(c, address: list[str] | None, topics0: list[str], start: int, end: int):
    """Paginate with next_block until `end` (exclusive). Returns (logs, n_requests)."""
    out, cur, n = [], start, 0
    while cur < end:
        q = Query(from_block=cur, to_block=end,
                  logs=[LogSelection(address=address, topics=[topics0])],
                  field_selection=FieldSelection(log=ALL_FIELDS))
        r = await c.get(q)
        n += 1
        out += [to_rpc_shape(lg) for lg in r.data.logs]
        if r.next_block <= cur:          # defensive: never loop forever
            break
        cur = r.next_block
    return out, n


def tally_test(rpc: Rpc, gov: str, fam: str, rows: list[dict]) -> dict:
    """Compare summed vote weights per proposal with the contract's stored tallies."""
    props = [r for r in rows if r["event"] == "ProposalCreated"]
    sums = defaultdict(lambda: {"for": 0, "against": 0, "abstain": 0})
    other_support = defaultdict(int)
    seen = set()
    dup = 0
    for r in rows:
        if not r["event"].startswith("VoteCast"):
            continue
        key = (r["tx"], r["log_index"])
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        k = SUPPORT_KEY.get(r["support"])
        if k:
            sums[r["proposal_id"]][k] += int(r["weight"])
        else:
            other_support[r["proposal_id"]] += 1
    match, mismatch, unreadable = 0, [], 0
    for p in props:
        pid = p["proposal_id"]
        t = views.tallies(rpc, gov, fam, int(pid), "latest")
        if t is None:
            unreadable += 1
            continue
        if t == sums[pid]:
            match += 1
        else:
            mismatch.append({"proposal_id": pid[:20], "contract": {k: str(v) for k, v in t.items()},
                             "from_logs": {k: str(v) for k, v in sums[pid].items()},
                             "non_simple_votes": other_support.get(pid, 0)})
    return {"proposals": len(props), "votes": len(seen), "duplicate_log_keys": dup,
            "tallies_match": match, "mismatch": len(mismatch), "unreadable": unreadable,
            "mismatch_examples": mismatch[:5]}


async def main() -> None:
    token = os.environ.get("HYPERSYNC_TOKEN")
    if not token:
        raise SystemExit("set HYPERSYNC_TOKEN (free token from envio.dev)")
    report: dict = {"run_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    gov_topics = [TOPIC["ProposalCreated"], TOPIC["VoteCast"], TOPIC["VoteCastWithParams"], TOPIC["VoteCastAlpha"]]
    heights = {}

    print("1. connection")
    for ch, c in CHAINS.items():
        cl = client(ch, token)
        cid, h = await cl.get_chain_id(), await cl.get_height()
        heights[ch] = h
        ok = cid == c["chain_id"]
        report[f"1_{ch}"] = {"chain_id": cid, "height": h, "pass": ok}
        print(f"   {ch}: chain_id={cid} height={h} {'PASS' if ok else 'FAIL'}")

    print("2. history depth (Compound Governor Alpha, 2020)")
    eth = client("ethereum", token)
    logs, n = await fetch_logs(eth, [COMPOUND_ALPHA], [TOPIC["ProposalCreated"]], 0, heights["ethereum"])
    rows = [decode_log(lg) for lg in logs]
    first = min((r["block"] for r in rows), default=None)
    rpc_eth = Rpc(CHAINS["ethereum"]["rpc"], min_interval=0.1)
    raw = rpc_eth.eth_call(COMPOUND_ALPHA, "0xda35c664")   # proposalCount()
    count_contract = int(raw, 16)
    ok = first is not None and first < 10_000_000 and len(rows) == count_contract
    report["2_history"] = {"proposals_in_logs": len(rows), "proposalCount_contract": count_contract,
                           "first_block": first, "pass": ok}
    print(f"   proposals in logs={len(rows)} contract proposalCount={count_contract} "
          f"first block={first} {'PASS' if ok else 'FAIL'}")

    for label, ch, gov, fam, expect in [
        ("3_uniswap_bravo", "ethereum", UNISWAP_BRAVO, "bravo", True),
        ("4_arbitrum_core", "arbitrum", ARB_CORE, "oz", True),
        ("5_optimism_agora", "optimism", OP_GOVERNOR, "oz", False),
    ]:
        print(f"{label[0]}. completeness: {label[2:]}")
        cl = client(ch, token)
        t0 = time.time()
        logs, n = await fetch_logs(cl, [gov], gov_topics, 0, heights[ch])
        rows = [r for r in (decode_log(lg) for lg in logs) if r]
        rpc = Rpc(CHAINS[ch]["rpc"], min_interval=0.1)
        res = tally_test(rpc, gov, fam, rows)
        res.update(seconds=round(time.time() - t0, 1), hypersync_requests=n)
        res["pass"] = (res["mismatch"] == 0 and res["unreadable"] == 0 and res["proposals"] > 0
                       and res["duplicate_log_keys"] == 0)
        if not expect and not res["pass"]:
            res["note"] = "expected: Agora modules (approval/optimistic) do not use simple counting"
        report[label] = res
        print(f"   proposals={res['proposals']} votes={res['votes']} match={res['tallies_match']} "
              f"mismatch={res['mismatch']} unreadable={res['unreadable']} "
              f"{'PASS' if res['pass'] else ('EXPECTED-DIFF' if not expect else 'FAIL')}")

    print("6. throughput: topic-only ProposalCreated scan, full history (part of the real frame)")
    for ch in CHAINS:
        cl = client(ch, token)
        t0 = time.time()
        logs, n = await fetch_logs(cl, None, [TOPIC["ProposalCreated"]], 0, heights[ch])
        secs = time.time() - t0
        contracts = len({lg["address"].lower() for lg in logs})
        try:
            rl = cl.rate_limit_info()
            rl = {"limit": rl.limit, "remaining": rl.remaining, "reset_secs": rl.reset_secs, "cost": rl.cost} if rl else None
        except Exception:
            rl = None
        report[f"6_{ch}"] = {"logs": len(logs), "contracts": contracts, "requests": n,
                             "seconds": round(secs, 1), "rate_limit": rl}
        print(f"   {ch}: {len(logs)} ProposalCreated logs from {contracts} contracts, "
              f"{n} requests, {secs:.0f}s, rate limit={rl}")

    out = ROOT / "pilot" / "pilot_report.json"
    out.write_text(json.dumps(report, indent=1))
    verdict = all(report[k]["pass"] for k in report if k[:2] in ("1_", "2_", "3_", "4_"))
    print(f"\nVERDICT: {'HyperSync is usable as the log source' if verdict else 'NOT confirmed — see pilot_report.json'}")
    print(f"report -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
