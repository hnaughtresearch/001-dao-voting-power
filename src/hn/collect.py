"""Step 3 · Per-governor extraction and contract reads (ordinary RPC, latest block).

For every governor of a DAO passing the prefilters:
  data/interim/<chain>/<governor>/logs.jsonl.gz    its logs, copied from the raw HyperSync pages
  data/interim/<chain>/<governor>/views.jsonl.gz   per proposal: state, tallies, quorum, snapshot
  data/interim/<chain>/<governor>/manifest.json    coverage (from the chain scan), family, token
Why the latest block is enough: Bravo and OZ governors store final tallies permanently; OZ
quorum(timepoint) reads historical checkpoints; the vote-stage result (passed vs defeated) of
a finished proposal does not change. Proposals still Pending/Active at read time are recorded
and excluded by `validate`. Resumable per proposal.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from .decode import decode_log
from .hs import chain_dir
from .rpc import Rpc
from .select import iter_raw, proposal_block_ts
from . import views

ROOT = Path(__file__).resolve().parents[2]


def _split(chain: str, govs: set[str]) -> None:
    """One pass over the raw pages, writing each selected governor's logs to its own file."""
    todo = {g for g in govs if not (ROOT / "data" / "interim" / chain / g / "logs.jsonl.gz").exists()}
    if not todo:
        return
    print(f"[{chain}] extracting logs for {len(todo)} governors …")
    part = {g: ROOT / "data" / "interim" / chain / g / "logs.jsonl.gz.part" for g in todo}
    for g, p in part.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.unlink(missing_ok=True)                    # discard leftovers of an interrupted run
    buf = defaultdict(list)

    def flush(g):
        with gzip.open(part[g], "at") as fh:         # appended gzip members read back as one stream
            fh.writelines(json.dumps(lg) + "\n" for lg in buf[g])
        buf[g].clear()

    for lg in iter_raw(chain):
        a = lg["address"].lower()
        if a in todo:
            buf[a].append(lg)
            if len(buf[a]) >= 20_000:
                flush(a)
    for g in todo:
        flush(g)
        part[g].replace(part[g].with_suffix(""))     # atomic: logs.jsonl.gz exists only when complete


def read_governor(cfg: dict, rpc: Rpc, chain: str, gov: str, cutoff_ts: int, ts: dict) -> None:
    d = ROOT / "data" / "interim" / chain / gov
    scan = json.loads((chain_dir(chain) / "manifest.json").read_text())
    created = []
    with gzip.open(d / "logs.jsonl.gz", "rt") as fh:
        for line in fh:
            r = decode_log(json.loads(line))
            if r and r["event"] == "ProposalCreated":
                r["created_ts"] = ts.get(r["block"]) or rpc.block_timestamp(r["block"])
                if r["created_ts"] < cutoff_ts:
                    created.append(r)
    fam, cm = views.family(rpc, gov)
    tok, getter = views.voting_token(rpc, gov)
    head = rpc.block_number()

    vp = d / "views.jsonl.gz"
    keep = []
    if vp.exists():                      # keep complete reads; re-read incomplete ones (repair mode)
        with gzip.open(vp, "rt") as fh:
            for l in fh:
                r = json.loads(l)
                if r.get("state") and "quorum_variants" in r:
                    keep.append(r)
        tmp = vp.with_suffix(".gz.tmp")
        with gzip.open(tmp, "wt") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in keep)
        tmp.replace(vp)
    done = {r["proposal_id"] for r in keep}
    with gzip.open(vp, "at") as out:
        for i, p in enumerate(created, 1):
            if p["proposal_id"] in done:
                continue
            pid = int(p["proposal_id"])
            snap = views.proposal_snapshot(rpc, gov, pid, "latest") if fam == "oz" else p["start_block"]
            t = views.tallies(rpc, gov, fam, pid, "latest")
            q = views.quorum(rpc, gov, fam, snap, "latest")
            words = views.struct_words(rpc, gov, pid, "latest")
            qv = views.quorum_variants(rpc, gov, pid, snap, "latest")
            out.write(json.dumps({
                "proposal_id": p["proposal_id"], "family": fam, "counting_mode": cm,
                "state": views.state(rpc, gov, pid, "latest"),
                "tallies": {k: str(v) for k, v in (t or {}).items()},
                "snapshot": snap, "quorum": None if q is None else str(q),
                "struct_words": None if words is None else [str(w) for w in words],
                "quorum_variants": {k: str(v) for k, v in qv.items()},
                "created_block": p["block"], "created_block_ts": p["created_ts"],
                "read_at_block": head,
            }) + "\n")
            print(f"   {gov[:10]}… views {i}/{len(created)}", end="\r")
    print()

    (d / "manifest.json").write_text(json.dumps({
        "chain": chain, "governor": gov, "family": fam, "counting_mode": cm, "token": tok,
        "token_getter": getter, "n_proposals_in_window": len(created),
        "start_block": 0, "cutoff_block": scan["scan_height"] - 1,
        "ranges": [[lo, hi - 1] for lo, hi in scan["ranges"]],     # inclusive form for validate
        "source": scan["source"], "rpc_host": rpc.url.split("/")[2],
        "cutoff_utc": cfg["cutoff_utc"],
        "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat()}, indent=1))


def run(cfg: dict, rpcs: dict[str, Rpc], only: list[str] | None) -> None:
    cand = ROOT / "data" / "processed" / "dao_candidates.json"
    if not cand.exists():
        raise SystemExit("missing dao_candidates.json: run `hn scan` and `hn select` first")
    cutoff_ts = int(dt.datetime.fromisoformat(cfg["cutoff_utc"]).replace(tzinfo=dt.timezone.utc).timestamp())
    daos = pd.read_json(cand)
    daos = daos[daos.passes_prefilters]
    if only:
        daos = daos[daos.dao_id.isin(only)]
    for ch, sub in daos.groupby("chain"):
        if ch not in rpcs:
            print(f"skip {ch}: no RPC")
            continue
        govs = {g for gl in sub.governors for g in gl}
        _split(ch, govs)
        ts = proposal_block_ts(ch)
        for d in sub.itertuples():
            print(f"== {d.dao_id} ({len(d.governors)} governor(s))")
            for g in d.governors:
                read_governor(cfg, rpcs[ch], ch, g, cutoff_ts, ts)
