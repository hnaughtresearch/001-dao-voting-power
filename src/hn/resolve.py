"""Step 2b · Infer and verify the voting token of governors whose token getter is unknown.

Method (data-driven, no guessing of function names):
  1. Take voters of the governor from the raw logs.
  2. Ask HyperSync which contracts emitted DelegateVotesChanged(delegate indexed, ...) for those
     voters. Compound-style and OZ ERC20Votes tokens both emit it. The candidate is the contract
     that covers the most sampled voters.
  3. Verify: for sampled votes, the candidate's getPriorVotes(voter, startBlock) (Bravo) or
     getPastVotes(voter, snapshot) (OZ) must equal the weight recorded in the VoteCast log.
A token is written to config/token_overrides.csv only if every verified vote matches exactly.
Only governors that could pass the governor-level filters are processed.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
from eth_abi import encode as abi_encode

from .constants import selector
from .decode import decode_log
from .hs import _client, _hs
from .rpc import Rpc, RpcError
from .select import iter_raw
from . import views

ROOT = Path(__file__).resolve().parents[2]
DVC = "0xdec2bacdd2f05b59de34da9b523dff8be42e5e38e818c82fdb0bae774387a724"  # DelegateVotesChanged(address,uint256,uint256)
SAMPLE_VOTERS = 40
VERIFY_VOTES = 8


def _addr_topic(a: str) -> str:
    return "0x" + "0" * 24 + a[2:].lower()


async def _candidates(url: str, token: str, voters: list[str], end: int) -> Counter:
    hs = _hs()
    c = _client(url, token)
    cover = defaultdict(set)
    cur = 0
    sel = hs.LogSelection(topics=[[DVC], [_addr_topic(v) for v in voters]])
    fields = hs.FieldSelection(log=[hs.LogField.ADDRESS, hs.LogField.TOPIC1])
    while cur < end:
        r = await c.get(hs.Query(from_block=cur, to_block=end, logs=[sel], field_selection=fields))
        for lg in r.data.logs:
            cover[lg.address.lower()].add(lg.topics[1][-40:].lower())
        if r.next_block <= cur:
            break
        cur = min(r.next_block, end)
    return Counter({a: len(s) for a, s in cover.items()})


def _votes_at(rpc: Rpc, tok: str, voter: str, block: int) -> int | None:
    for sig in ("getPriorVotes(address,uint256)", "getPastVotes(address,uint256)"):
        data = selector(sig) + abi_encode(["address", "uint256"], [voter, block]).hex()
        try:
            out = rpc.eth_call(tok, data, "latest")
        except RpcError:
            continue
        if out and out != "0x":
            return int(out, 16)
    return None


def _verify(rpc: Rpc, gov: str, tok: str, fam: str, props: dict, votes: list[dict]) -> tuple[int, int]:
    """(matched, checked) over sampled votes."""
    checked = matched = 0
    for v in votes[:VERIFY_VOTES * 3]:
        p = props.get(v["proposal_id"])
        if p is None:
            continue
        snaps = []
        if fam == "oz":
            snaps.append(views.proposal_snapshot(rpc, gov, int(v["proposal_id"]), "latest"))
        snaps += [p["start_block"], p["block"], p["start_block"] - 1]   # Bravo / creation-block forks
        ok = False
        for snap in [s for s in snaps if s is not None]:
            w = _votes_at(rpc, tok, v["voter"], snap)
            if w is not None and w == int(v["weight"]):
                ok = True
                break
        checked += 1
        matched += int(ok)
        if checked >= VERIFY_VOTES:
            break
    return matched, checked


def run(cfg: dict, rpcs: dict[str, Rpc], chains: list[str]) -> None:
    token = os.environ.get("HYPERSYNC_TOKEN")
    if not token:
        raise SystemExit("set HYPERSYNC_TOKEN")
    f = cfg["filters"]
    path = ROOT / "data" / "processed" / "unresolved_token.csv"
    if not path.exists():
        raise SystemExit("run `hn select` first")
    u = pd.read_csv(path)
    u["span_days"] = (u.last_ts - u.first_ts) / 86400
    u = u[(u.n_executed >= 1) & (u.median_votes_per_proposal >= int(f["min_median_voters"]))
          & (u.span_days >= int(f["min_span_days"]))]
    u = u[u.chain.isin(chains)]
    print(f"{len(u)} unresolved governors could pass the governor-level filters")

    ov_path = ROOT / "config" / "token_overrides.csv"
    existing = pd.read_csv(ov_path, dtype=str) if ov_path.exists() else pd.DataFrame(columns=["chain", "addr", "token", "note"])
    rows, report = existing.to_dict("records"), []
    done = {(r["chain"], str(r["addr"]).lower()) for r in rows}

    for ch, sub in u.groupby("chain"):
        rpc = rpcs[ch]
        scan = json.loads((ROOT / "data" / "raw" / "hypersync" / ch / "manifest.json").read_text())
        targets = set(sub.addr) - {a for c, a in done if c == ch}
        if not targets:
            continue
        logs = defaultdict(list)
        for lg in iter_raw(ch):
            a = lg["address"].lower()
            if a in targets:
                r = decode_log(lg)
                if r:
                    logs[a].append(r)
        for gov in sorted(targets):
            rs = logs[gov]
            props = {r["proposal_id"]: r for r in rs if r["event"] == "ProposalCreated"}
            votes = [r for r in rs if r["event"] in ("VoteCast", "VoteCastWithParams", "VoteCastAlpha")
                     and int(r["weight"]) > 0]
            voters = list(dict.fromkeys(v["voter"] for v in votes))[:SAMPLE_VOTERS]
            fam, _ = views.family(rpc, gov)
            cand = asyncio.run(_candidates(cfg["chains"][ch]["hypersync_url"], token, voters, scan["scan_height"]))
            status, tok, note = "unresolved", None, ""
            for addr, n in cand.most_common(3):
                m, k = _verify(rpc, gov, addr, fam, props, votes)
                if k > 0 and m == k:
                    tok, status = addr, "verified"
                    note = f"inferred via DelegateVotesChanged ({n}/{len(voters)} voters); verified {m}/{k} votes"
                    break
                note += f"{addr[:10]}… {n}/{len(voters)} voters, verify {m}/{k}; "
            report.append({"chain": ch, "addr": gov, "family": fam, "status": status, "token": tok, "note": note})
            print(f"  {gov}: {status} {tok or ''} {note}")
            if tok:
                rows.append({"chain": ch, "addr": gov, "token": tok, "note": note})

    ov_path.parent.mkdir(exist_ok=True)
    with open(ov_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["chain", "addr", "token", "note"])
        w.writeheader()
        w.writerows(rows)
    pd.DataFrame(report).to_csv(ROOT / "data" / "processed" / "token_resolution_report.csv", index=False)
    print(f"\n{sum(r['status'] == 'verified' for r in report)}/{len(report)} verified → config/token_overrides.csv")
    print("Now re-run: py -m hn select")
