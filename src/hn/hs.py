"""Step 1 · Full-chain scan of governor events with Envio HyperSync (free tier, no card).

Writes raw pages exactly as received (converted to the eth_getLogs JSON shape):
  data/raw/hypersync/<chain>/events/part-<from>-<to>.jsonl.gz   all governor-family logs
  data/raw/hypersync/<chain>/proposal_blocks.jsonl.gz           block number -> timestamp for
                                                                every ProposalCreated block
  data/raw/hypersync/<chain>/manifest.json                      covered ranges, counts, hashes
The scan runs to the HyperSync height at scan time (not to the cutoff) so that stored on-chain
tallies, read later at the latest block, include every vote. The study cutoff is applied to
proposal creation time in `select`. Resumable: re-running continues from the last page.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path

from .constants import ALL_TOPICS, TOPIC

ROOT = Path(__file__).resolve().parents[2]


def _hs():
    import hypersync  # imported lazily: only the scan needs it
    return hypersync


def to_rpc_shape(lg) -> dict:
    """HyperSync Log -> the eth_getLogs dict shape that hn.decode expects."""
    topics = [t for t in (lg.topics or []) if t]
    return {"address": lg.address, "topics": topics, "data": lg.data or "0x",
            "blockNumber": hex(lg.block_number), "transactionHash": lg.transaction_hash,
            "logIndex": hex(lg.log_index)}


def _int(x) -> int:
    if isinstance(x, int):
        return x
    return int(x, 16) if str(x).startswith("0x") else int(x)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def chain_dir(chain: str) -> Path:
    return ROOT / "data" / "raw" / "hypersync" / chain


def _client(url: str, token: str):
    hs = _hs()
    return hs.HypersyncClient(hs.ClientConfig(url=url, api_token=token, max_num_retries=12,
                                              proactive_rate_limit_sleep=True))


async def _scan_events(chain: str, url: str, token: str) -> None:
    hs = _hs()
    d = chain_dir(chain)
    (d / "events").mkdir(parents=True, exist_ok=True)
    man_p = d / "manifest.json"
    man = json.loads(man_p.read_text()) if man_p.exists() else {
        "chain": chain, "source": url, "topics": ALL_TOPICS, "ranges": [], "n_logs": 0, "files": {}}
    c = _client(url, token)
    height = await c.get_height()
    if "scan_height" not in man:
        man["scan_height"] = height          # frozen at the first run; resumes go to the same height
    end = man["scan_height"]
    cur = man["ranges"][-1][1] if man["ranges"] else 0
    fields = hs.FieldSelection(log=[hs.LogField.ADDRESS, hs.LogField.DATA, hs.LogField.TOPIC0,
                                    hs.LogField.TOPIC1, hs.LogField.TOPIC2, hs.LogField.TOPIC3,
                                    hs.LogField.BLOCK_NUMBER, hs.LogField.TRANSACTION_HASH,
                                    hs.LogField.LOG_INDEX])
    while cur < end:
        q = hs.Query(from_block=cur, to_block=end, field_selection=fields,
                     logs=[hs.LogSelection(topics=[ALL_TOPICS])])
        r = await c.get(q)
        nxt = min(r.next_block, end)
        if nxt <= cur:
            raise SystemExit(f"[{chain}] HyperSync returned no progress at block {cur}")
        name = f"part-{cur:012d}-{nxt:012d}.jsonl.gz"
        path = d / "events" / name
        with gzip.open(path, "wt") as fh:
            for lg in r.data.logs:
                fh.write(json.dumps(to_rpc_shape(lg)) + "\n")
        man["ranges"].append([cur, nxt])                 # half-open [cur, nxt)
        man["n_logs"] += len(r.data.logs)
        man["files"][name] = _sha256(path)
        man_p.write_text(json.dumps(man))                # checkpoint after every page
        print(f"  [{chain}] blocks {cur:,}–{nxt:,} of {end:,}: +{len(r.data.logs)} logs "
              f"(total {man['n_logs']:,})", end="\r")
        cur = nxt
    print()

    # ProposalCreated block timestamps (small second pass; blocks are joined to returned logs).
    bp = d / "proposal_blocks.jsonl.gz"
    if not man.get("proposal_blocks_done"):
        cur, n = 0, 0
        bfields = hs.FieldSelection(log=[hs.LogField.BLOCK_NUMBER],
                                    block=[hs.BlockField.NUMBER, hs.BlockField.TIMESTAMP])
        with gzip.open(bp, "wt") as fh:
            while cur < end:
                q = hs.Query(from_block=cur, to_block=end, field_selection=bfields,
                             logs=[hs.LogSelection(topics=[[TOPIC["ProposalCreated"]]])])
                r = await c.get(q)
                for b in r.data.blocks:
                    fh.write(json.dumps({"block": _int(b.number), "timestamp": _int(b.timestamp)}) + "\n")
                    n += 1
                if r.next_block <= cur:
                    break
                cur = min(r.next_block, end)
        man.update(proposal_blocks_done=True, n_proposal_blocks=n, proposal_blocks_sha256=_sha256(bp))
    man["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    man_p.write_text(json.dumps(man, indent=1))
    print(f"[{chain}] done: {man['n_logs']:,} logs, {man.get('n_proposal_blocks', 0):,} proposal blocks")


def run(cfg: dict, chains: list[str]) -> None:
    token = os.environ.get("HYPERSYNC_TOKEN")
    if not token:
        raise SystemExit("set HYPERSYNC_TOKEN (free token from envio.dev)")
    for ch in chains:
        print(f"== scan {ch}")
        asyncio.run(_scan_events(ch, cfg["chains"][ch]["hypersync_url"], token))
