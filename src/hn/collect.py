"""Step 3 · Extract raw logs and contract state by RPC. Raw data is written once, never edited.

Layout:
  data/raw/<chain>/<governor>/logs.jsonl.gz      every raw log, as returned by eth_getLogs
  data/raw/<chain>/<governor>/manifest.json      covered block ranges, counts, sha256, endpoints
  data/raw/<chain>/<governor>/views.jsonl.gz     per-proposal contract reads at the cutoff block
Resumable: re-running continues from the last covered block.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from .constants import ALL_TOPICS
from .decode import decode_log
from .rpc import Rpc, block_at_or_before
from . import views

ROOT = Path(__file__).resolve().parents[2]


def cutoff_blocks(cfg: dict, rpcs: dict[str, Rpc]) -> dict[str, int]:
    path = ROOT / "data" / "raw" / "cutoff_blocks.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    ts = int(dt.datetime.fromisoformat(cfg["cutoff_utc"]).replace(tzinfo=dt.timezone.utc).timestamp())
    for chain, rpc in rpcs.items():
        key = f"{chain}@{cfg['cutoff_utc']}"
        if key not in cache:
            cache[key] = block_at_or_before(rpc, ts - 1)
            print(f"[{chain}] cutoff block {cache[key]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2))
    return {c: cache[f"{c}@{cfg['cutoff_utc']}"] for c in rpcs}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_governor(cfg: dict, rpc: Rpc, chain: str, gov: str, start: int, cutoff: int) -> None:
    d = ROOT / "data" / "raw" / chain / gov
    d.mkdir(parents=True, exist_ok=True)
    man_p = d / "manifest.json"
    man = json.loads(man_p.read_text()) if man_p.exists() else {
        "chain": chain, "governor": gov, "cutoff_utc": cfg["cutoff_utc"], "cutoff_block": cutoff,
        "start_block": start, "ranges": [], "n_logs": 0}
    if man["cutoff_block"] != cutoff:
        raise SystemExit(f"{gov}: manifest cutoff {man['cutoff_block']} != {cutoff}; use a fresh data dir")
    cur = man["ranges"][-1][1] + 1 if man["ranges"] else start

    if cur <= cutoff:
        with gzip.open(d / "logs.jsonl.gz", "at") as out:
            for lo, hi, logs in rpc.get_logs(gov, ALL_TOPICS, cur, cutoff, int(cfg["rpc"]["log_chunk_blocks"][chain])):
                for lg in logs:
                    out.write(json.dumps(lg) + "\n")
                man["ranges"].append([lo, hi])
                man["n_logs"] += len(logs)
                man_p.write_text(json.dumps(man))       # checkpoint after every chunk
                print(f"  {gov} blocks {lo}-{hi}: {len(logs)} logs", end="\r")
        print()

    # Contract reads per proposal, at the cutoff block (needs an archive node for past blocks).
    created = []
    with gzip.open(d / "logs.jsonl.gz", "rt") as fh:
        for line in fh:
            row = decode_log(json.loads(line))
            if row and row["event"] == "ProposalCreated":
                created.append(row)
    fam, cm = views.family(rpc, gov, cutoff)
    tok, getter = views.voting_token(rpc, gov, cutoff)
    done = set()
    vp = d / "views.jsonl.gz"
    if vp.exists():
        with gzip.open(vp, "rt") as fh:
            done = {json.loads(l)["proposal_id"] for l in fh}
    with gzip.open(vp, "at") as out:
        for i, p in enumerate(created):
            if p["proposal_id"] in done:
                continue
            pid = int(p["proposal_id"])
            snap = views.proposal_snapshot(rpc, gov, pid, cutoff) if fam == "oz" else p["start_block"]
            rec = {
                "proposal_id": p["proposal_id"], "family": fam, "counting_mode": cm,
                "state": views.state(rpc, gov, pid, cutoff),
                "tallies": {k: str(v) for k, v in (views.tallies(rpc, gov, fam, pid, cutoff) or {}).items()},
                "snapshot": snap,
                "quorum": str(views.quorum(rpc, gov, fam, snap, cutoff)),
                "created_block_ts": rpc.block_timestamp(p["block"]),
                "read_at_block": cutoff,
            }
            out.write(json.dumps(rec) + "\n")
            print(f"  views {i + 1}/{len(created)}", end="\r")
    print()

    man.update(family=fam, counting_mode=cm, token=tok, token_getter=getter,
               n_proposals=len(created), logs_sha256=_sha256(d / "logs.jsonl.gz"),
               views_sha256=_sha256(vp), rpc_host=rpc.url.split("/")[2],
               finished_utc=dt.datetime.now(dt.timezone.utc).isoformat())
    man_p.write_text(json.dumps(man, indent=1))


def run(cfg: dict, rpcs: dict[str, Rpc], only: list[str] | None) -> None:
    cand = ROOT / "data" / "processed" / "dao_candidates.json"
    if not cand.exists():
        raise SystemExit("missing dao_candidates.json: run `hn frame --execute` and `hn select` first")
    if not rpcs:
        raise SystemExit("no RPC endpoints: set ETH_RPC_URL / ARB_RPC_URL / OP_RPC_URL")
    cut = cutoff_blocks(cfg, rpcs)
    daos = pd.read_json(cand)
    daos = daos[daos.passes_prefilters]
    if only:
        daos = daos[daos.dao_id.isin(only)]
    frame = pd.concat([pd.read_parquet(p) for p in (ROOT / "data" / "raw" / "frame").glob("*.parquet")])
    first = {(r.chain, r.addr): int(r.first_block) for r in frame.itertuples()}
    for d in daos.itertuples():
        if d.chain not in rpcs:
            continue
        print(f"== {d.dao_id} ({len(d.governors)} governor(s))")
        for g in d.governors:
            collect_governor(cfg, rpcs[d.chain], d.chain, g, first[(d.chain, g)], cut[d.chain])
