"""Step 2 · Group governors into DAOs by voting token and apply the pre-registered prefilters.

Prefilters use BigQuery counts (cheap upper bounds). The exact filters (decided proposals by
state(), median voters per decided proposal) are re-applied in `validate` after collection.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .rpc import Rpc
from .views import voting_token, family

ROOT = Path(__file__).resolve().parents[2]


def run(cfg: dict, rpcs: dict[str, Rpc], chains: list[str]) -> None:
    f = cfg["filters"]
    frames = []
    for chain in chains:
        p = ROOT / "data" / "raw" / "frame" / f"{chain}.parquet"
        if not p.exists():
            raise SystemExit(f"missing {p}: run `hn frame --execute` first")
        frames.append(pd.read_parquet(p))
    gov = pd.concat(frames, ignore_index=True)
    funnel = [("contracts_with_vote_events", len(gov))]

    nonstd = gov[gov.n_created_std == 0]
    nonstd.to_csv(ROOT / "data" / "processed" / "nonstandard_vote_contracts.csv", index=False)
    funnel.append(("of_which_no_standard_ProposalCreated (Tier B review)", len(nonstd)))

    # Light governor-level prefilter only to limit RPC calls; DAO-level filters come after grouping.
    light = gov[(gov.n_created_std >= int(f["light_min_created_per_governor"])) & (gov.n_votes > 0)].copy()
    funnel.append((f"governors_with_>={f['light_min_created_per_governor']}_proposals", len(light)))

    rows = []
    for chain in chains:
        rpc = rpcs[chain]
        sub = light[light.chain == chain]
        print(f"[{chain}] resolving voting token for {len(sub)} governors …")
        for addr in sub.addr:
            tok, getter = voting_token(rpc, addr)
            fam, cm = family(rpc, addr)
            rows.append({"chain": chain, "addr": addr, "token": tok, "token_getter": getter,
                         "family": fam, "counting_mode": cm})
    meta = pd.DataFrame(rows)
    light = light.merge(meta, on=["chain", "addr"], how="left")

    # Manual overrides for governors whose token getter is non-standard (documented, versioned).
    ov_path = ROOT / "config" / "token_overrides.csv"
    if ov_path.exists():
        ov = pd.read_csv(ov_path, dtype=str)
        for _, r in ov.iterrows():
            m = (light.chain == r.chain) & (light.addr == r.addr.lower())
            light.loc[m, "token"] = r.token.lower()
            light.loc[m, "token_getter"] = "override"
    unresolved = light[light.token.isna()]
    unresolved.to_csv(ROOT / "data" / "processed" / "unresolved_token.csv", index=False)
    funnel.append(("governors_token_unresolved (see unresolved_token.csv)", len(unresolved)))
    light = light[light.token.notna()]

    light["dao_id"] = light.chain + ":" + light.token
    dao = light.groupby("dao_id").agg(
        chain=("chain", "first"), token=("token", "first"), governors=("addr", list),
        families=("family", lambda s: sorted(set(map(str, s)))),
        n_created_std=("n_created_std", "sum"), n_executed=("n_executed", "sum"),
        first_ts=("first_ts", "min"), last_ts=("last_ts", "max"),
        median_voters_main=("median_voters", lambda s: s.loc[light.loc[s.index, "n_created_std"].idxmax()]),
    ).reset_index()
    funnel.append(("daos_after_grouping_by_token", len(dao)))

    span_days = (pd.to_datetime(dao.last_ts) - pd.to_datetime(dao.first_ts)).dt.days
    steps = [
        ("filter1_binding_executed>=1", dao.n_executed >= 1),
        (f"filter2_created>={f['min_decided']} (upper bound of decided)", dao.n_created_std >= int(f["min_decided"])),
        (f"filter3_median_voters>={f['min_median_voters']} (main governor)", dao.median_voters_main >= int(f["min_median_voters"])),
        (f"filter4_span>={f['min_span_days']}d", span_days >= int(f["min_span_days"])),
    ]
    keep = pd.Series(True, index=dao.index)
    for name, cond in steps:
        keep &= cond
        funnel.append((name, int(keep.sum())))
    dao["passes_prefilters"] = keep

    out = ROOT / "data" / "processed"
    dao.to_json(out / "dao_candidates.json", orient="records", indent=1, date_format="iso")
    pd.DataFrame(funnel, columns=["step", "count"]).to_csv(out / "funnel_prefilter.csv", index=False)
    print(pd.DataFrame(funnel, columns=["step", "count"]).to_string(index=False))
    print(f"\n{int(keep.sum())} DAOs pass prefilters → data/processed/dao_candidates.json")
