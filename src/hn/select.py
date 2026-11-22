"""Step 2 · Frame summary from raw HyperSync logs, prefilters, grouping into DAOs by voting token.

Only proposals created before the study cutoff count. Prefilters use event counts; the exact
filters (decided proposals by state(), distinct voters) are re-applied in `validate`.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from .constants import TOPIC_TO_NAME
from .hs import chain_dir
from .rpc import Rpc
from .views import voting_token, family

ROOT = Path(__file__).resolve().parents[2]


def iter_raw(chain: str):
    """Yield raw log dicts for a chain, in block order."""
    for p in sorted((chain_dir(chain) / "events").glob("part-*.jsonl.gz")):
        with gzip.open(p, "rt") as fh:
            for line in fh:
                yield json.loads(line)


def proposal_block_ts(chain: str) -> dict[int, int]:
    p = chain_dir(chain) / "proposal_blocks.jsonl.gz"
    out = {}
    if p.exists():
        with gzip.open(p, "rt") as fh:
            for line in fh:
                r = json.loads(line)
                out[r["block"]] = r["timestamp"]
    return out


def _pid_and_voter(lg: dict, name: str) -> tuple[str, str]:
    data = lg["data"]
    if name == "VoteCastAlpha":       # nothing indexed: voter, proposalId in data words 0-1
        return str(int(data[66:130], 16)), "0x" + data[26:66].lower()
    return str(int(data[2:66], 16)), "0x" + lg["topics"][1][-40:].lower()


def summarise_chain(chain: str, cutoff_ts: int, rpc: Rpc | None) -> pd.DataFrame:
    """Per-contract counts, restricted to proposals created before the cutoff."""
    ts = proposal_block_ts(chain)
    created = defaultdict(dict)            # addr -> {pid: created_ts}
    votes = defaultdict(lambda: defaultdict(int))   # addr -> pid -> n votes
    execs, cancels, nvotes_all = defaultdict(set), defaultdict(set), defaultdict(int)
    missing_ts = 0
    for lg in iter_raw(chain):
        name = TOPIC_TO_NAME.get(lg["topics"][0].lower()) if lg["topics"] else None
        if name is None:
            continue
        a = lg["address"].lower()
        if name == "ProposalCreated":
            b = int(lg["blockNumber"], 16)
            t = ts.get(b)
            if t is None and rpc is not None:          # fallback if the block join was incomplete
                t = ts[b] = rpc.block_timestamp(b)
                missing_ts += 1
            created[a][str(int(lg["data"][2:66], 16))] = t
        elif name.startswith("VoteCast"):
            pid, _ = _pid_and_voter(lg, name)
            votes[a][pid] += 1
            nvotes_all[a] += 1
        elif name == "ProposalExecuted":
            execs[a].add(str(int(lg["data"][2:66], 16)))
        elif name == "ProposalCanceled":
            cancels[a].add(str(int(lg["data"][2:66], 16)))
    if missing_ts:
        print(f"[{chain}] {missing_ts} proposal timestamps fetched by RPC (block join fallback)")

    rows = []
    for a in set(created) | set(nvotes_all):
        in_window = {p: t for p, t in created.get(a, {}).items() if t is not None and t < cutoff_ts}
        vc = [votes[a].get(p, 0) for p in in_window]
        rows.append({
            "chain": chain, "addr": a,
            "n_created_std": len(in_window),
            "n_executed": len(execs[a] & set(in_window)),
            "n_canceled": len(cancels[a] & set(in_window)),
            "n_votes": nvotes_all[a],
            "median_votes_per_proposal": float(pd.Series(vc).median()) if vc else 0.0,
            "first_ts": min(in_window.values()) if in_window else None,
            "last_ts": max(in_window.values()) if in_window else None,
        })
    return pd.DataFrame(rows)


def run(cfg: dict, rpcs: dict[str, Rpc], chains: list[str]) -> None:
    f = cfg["filters"]
    cutoff_ts = int(dt.datetime.fromisoformat(cfg["cutoff_utc"]).replace(tzinfo=dt.timezone.utc).timestamp())
    out = ROOT / "data" / "processed"
    out.mkdir(parents=True, exist_ok=True)

    frames = []
    for ch in chains:
        if not (chain_dir(ch) / "manifest.json").exists():
            raise SystemExit(f"no scan for {ch}: run `hn scan` first")
        print(f"[{ch}] summarising raw logs …")
        df = summarise_chain(ch, cutoff_ts, rpcs.get(ch))
        df.to_parquet(out / f"frame_{ch}.parquet", index=False)
        frames.append(df)
    gov = pd.concat(frames, ignore_index=True)
    funnel = [("contracts_with_governor_events", len(gov))]

    nonstd = gov[(gov.n_created_std == 0) & (gov.n_votes > 0)]
    nonstd.to_csv(out / "nonstandard_vote_contracts.csv", index=False)
    funnel.append(("of_which_votes_but_no_standard_ProposalCreated (Tier B review)", len(nonstd)))

    light = gov[(gov.n_created_std >= int(f["light_min_created_per_governor"])) & (gov.n_votes > 0)].copy()
    funnel.append((f"governors_with_>={f['light_min_created_per_governor']}_proposals_before_cutoff", len(light)))

    meta = []
    for ch in chains:
        rpc = rpcs.get(ch)
        sub = light[light.chain == ch]
        if rpc is None:
            print(f"[{ch}] no RPC: tokens not resolved")
            continue
        print(f"[{ch}] resolving voting token for {len(sub)} governors …")
        for i, addr in enumerate(sub.addr, 1):
            tok, getter = voting_token(rpc, addr)
            fam, cm = family(rpc, addr)
            meta.append({"chain": ch, "addr": addr, "token": tok, "token_getter": getter,
                         "family": fam, "counting_mode": cm})
            print(f"   {i}/{len(sub)}", end="\r")
        print()
    light = light.merge(pd.DataFrame(meta), on=["chain", "addr"], how="left")

    ov_path = ROOT / "config" / "token_overrides.csv"
    if ov_path.exists():
        ov = pd.read_csv(ov_path, dtype=str)
        for _, r in ov.iterrows():
            m = (light.chain == r.chain) & (light.addr == r.addr.lower())
            light.loc[m, "token"] = r.token.lower()
            light.loc[m, "token_getter"] = "override"
    unresolved = light[light.token.isna()]
    unresolved.to_csv(out / "unresolved_token.csv", index=False)
    funnel.append(("governors_token_unresolved (see unresolved_token.csv)", len(unresolved)))
    light = light[light.token.notna()].copy()

    light["dao_id"] = light.chain + ":" + light.token
    main_gov = light.sort_values("n_created_std").groupby("dao_id").tail(1).set_index("dao_id")
    dao = light.groupby("dao_id").agg(
        chain=("chain", "first"), token=("token", "first"), governors=("addr", list),
        families=("family", lambda s: sorted(set(map(str, s)))),
        n_created_std=("n_created_std", "sum"), n_executed=("n_executed", "sum"),
        first_ts=("first_ts", "min"), last_ts=("last_ts", "max"),
    )
    dao["median_votes_main"] = main_gov["median_votes_per_proposal"]
    dao = dao.reset_index()
    funnel.append(("daos_after_grouping_by_token", len(dao)))

    span_days = (dao.last_ts - dao.first_ts) / 86400
    steps = [
        ("filter1_binding_executed>=1", dao.n_executed >= 1),
        (f"filter2_created>={f['min_decided']} (upper bound of decided)", dao.n_created_std >= int(f["min_decided"])),
        (f"filter3_median_votes>={f['min_median_voters']} (main governor)",
         dao.median_votes_main >= int(f["min_median_voters"])),
        (f"filter4_span>={f['min_span_days']}d", span_days >= int(f["min_span_days"])),
    ]
    keep = pd.Series(True, index=dao.index)
    for name, cond in steps:
        keep &= cond
        funnel.append((name, int(keep.sum())))
    dao["passes_prefilters"] = keep

    dao.to_json(out / "dao_candidates.json", orient="records", indent=1)
    fdf = pd.DataFrame(funnel, columns=["step", "count"])
    fdf.to_csv(out / "funnel_prefilter.csv", index=False)
    print(fdf.to_string(index=False))
    print(f"\n{int(keep.sum())} DAOs pass prefilters → data/processed/dao_candidates.json")
