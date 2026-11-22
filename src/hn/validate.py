"""Step 4 · Decode per-governor data into processed tables, run validation gate, assign tiers.

Outputs (data/processed/):
  proposals.parquet, votes.parquet         decoded, de-duplicated
  validation_proposals.csv                 per-proposal check results
  validation_governors.csv                 per-governor summary and tier
  funnel_final.csv                         exact filters after collection
"""
from __future__ import annotations

import gzip
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

from .constants import DECIDED_STATES, PASSED_STATES, AGAINST, FOR, ABSTAIN
from .decode import decode_log
from .outcome import quorum_basis, passes

ROOT = Path(__file__).resolve().parents[2]
SUPPORT_KEY = {AGAINST: "against", FOR: "for", ABSTAIN: "abstain"}


def _load_governor(d: Path):
    man = json.loads((d / "manifest.json").read_text())
    seen, rows = set(), []
    with gzip.open(d / "logs.jsonl.gz", "rt") as fh:
        for line in fh:
            lg = json.loads(line)
            key = (lg["transactionHash"], lg["logIndex"])
            if key in seen:            # duplicates can appear after an interrupted, resumed chunk
                continue
            seen.add(key)
            r = decode_log(lg)
            if r:
                rows.append(r)
    views = {}
    vp = d / "views.jsonl.gz"
    if vp.exists():
        with gzip.open(vp, "rt") as fh:
            for line in fh:
                v = json.loads(line)
                views[v["proposal_id"]] = v
    return man, rows, views


def _gaps(ranges: list, start: int, end: int) -> int:
    """Number of uncovered blocks between start and end."""
    covered, cur, missing = sorted(ranges), start, 0
    for lo, hi in covered:
        if lo > cur:
            missing += lo - cur
        cur = max(cur, hi + 1)
    return missing + max(0, end + 1 - cur)


def run(cfg: dict) -> None:
    f = cfg["filters"]
    out = ROOT / "data" / "processed"
    all_p, all_v, prow, grow = [], [], [], []

    for d in sorted((ROOT / "data" / "interim").glob("*/0x*")):
        man, rows, views = _load_governor(d)
        chain, gov = man["chain"], man["governor"]
        fam, cm = man.get("family"), man.get("counting_mode")
        basis = quorum_basis(fam, cm)
        # Study window: only proposals that have contract reads (created before the cutoff).
        props = [r for r in rows if r["event"] == "ProposalCreated" and r["proposal_id"] in views]
        votes = [r for r in rows if r["event"] in ("VoteCast", "VoteCastWithParams", "VoteCastAlpha")]
        n_unparsed = sum(1 for r in rows if r["event"].endswith("_unparsed"))
        for r in props + votes:
            r["chain"] = chain
        all_p += props
        all_v += votes

        # Duplicate (proposal, voter) pairs are invalid in Bravo and OZ simple counting.
        pair_count = defaultdict(int)
        sums = defaultdict(lambda: {"for": 0, "against": 0, "abstain": 0})
        params_used = defaultdict(bool)
        voters_by_pid = defaultdict(set)
        for v in votes:
            pair_count[(v["proposal_id"], v["voter"])] += 1
            voters_by_pid[v["proposal_id"]].add(v["voter"])
            if v.get("has_params"):
                params_used[v["proposal_id"]] = True
            k = SUPPORT_KEY.get(v["support"])
            if k:
                sums[v["proposal_id"]][k] += int(v["weight"])
        dup_pids = {pid for (pid, _), n in pair_count.items() if n > 1}

        n_dec = n_tally_ok = n_out_ok = 0
        voters_per_decided = []
        for p in props:
            pid = p["proposal_id"]
            vw = views.get(pid, {})
            st = vw.get("state")
            decided = st in DECIDED_STATES
            t_contract = {k: int(x) for k, x in vw.get("tallies", {}).items()} or None
            t_events = sums[pid]
            tally_ok = t_contract is not None and t_contract == t_events
            q = vw.get("quorum")
            q = int(q) if q not in (None, "None") else None
            if decided and basis and t_contract is not None and q is not None:
                predicted = passes(t_contract, q, basis, fam)
                outcome_ok = predicted == (st in PASSED_STATES)
            else:
                outcome_ok = None
            n_voters = len(voters_by_pid[pid])
            if decided:
                n_dec += 1
                n_tally_ok += bool(tally_ok)
                n_out_ok += bool(outcome_ok)
                voters_per_decided.append(n_voters)
            prow.append({"chain": chain, "governor": gov, "proposal_id": pid, "state": st,
                         "decided": decided, "n_voters": n_voters, "tally_match": tally_ok,
                         "outcome_match": outcome_ok, "duplicate_voters": pid in dup_pids,
                         "uses_vote_params": params_used[pid], "quorum": q,
                         "created_ts": vw.get("created_block_ts")})

        gaps = _gaps(man["ranges"], man["start_block"], man["cutoff_block"])
        generic_ok = (n_dec > 0 and basis is not None and n_tally_ok == n_dec and n_out_ok == n_dec
                      and not dup_pids and gaps == 0 and n_unparsed == 0)
        grow.append({"chain": chain, "governor": gov, "token": man.get("token"), "family": fam,
                     "counting_mode": cm, "quorum_basis": ",".join(basis) if basis else None,
                     "n_proposals": len(props), "n_decided": n_dec,
                     "tally_match": f"{n_tally_ok}/{n_dec}", "outcome_match": f"{n_out_ok}/{n_dec}",
                     "duplicate_pairs": len(dup_pids), "uncovered_blocks": gaps,
                     "unparsed_logs": n_unparsed,
                     "median_voters_decided": pd.Series(voters_per_decided).median() if voters_per_decided else 0,
                     "generic_rule_reproduces": generic_ok})

    pd.DataFrame(all_p).to_parquet(out / "proposals.parquet", index=False)
    vdf = pd.DataFrame(all_v)
    vdf.to_parquet(out / "votes.parquet", index=False)   # weight stored as string: uint256 > int64
    pv = pd.DataFrame(prow)
    pv.to_csv(out / "validation_proposals.csv", index=False)
    gv = pd.DataFrame(grow)

    # DAO-level exact filters and tiers (a DAO is the set of governors sharing a token).
    gv["dao_id"] = gv.chain + ":" + gv.token.fillna("unknown")
    dec = pv[pv.decided].merge(gv[["chain", "governor", "dao_id"]], on=["chain", "governor"])
    dao = dec.groupby("dao_id").agg(n_decided=("proposal_id", "count"),
                                    median_voters=("n_voters", "median"),
                                    first_ts=("created_ts", "min"), last_ts=("created_ts", "max"))
    dao["span_days"] = (dao.last_ts - dao.first_ts) / 86400
    dao["generic_all_governors"] = gv.groupby("dao_id").generic_rule_reproduces.all()
    funnel = [("daos_collected", len(dao))]
    keep = pd.Series(True, index=dao.index)
    for name, cond in [
        (f"exact_decided>={f['min_decided']}", dao.n_decided >= int(f["min_decided"])),
        (f"exact_median_voters>={f['min_median_voters']}", dao.median_voters >= int(f["min_median_voters"])),
        (f"exact_span>={f['min_span_days']}d", dao.span_days >= int(f["min_span_days"])),
    ]:
        keep &= cond
        funnel.append((name, int(keep.sum())))
    dao["passes_exact_filters"] = keep
    dao["tier"] = "excluded"
    dao.loc[keep & dao.generic_all_governors, "tier"] = "A"
    dao.loc[keep & ~dao.generic_all_governors, "tier"] = "B"
    funnel += [("tier_A", int((dao.tier == "A").sum())), ("tier_B", int((dao.tier == "B").sum()))]

    gv.to_csv(out / "validation_governors.csv", index=False)
    dao.reset_index().to_csv(out / "daos_final.csv", index=False)
    pd.DataFrame(funnel, columns=["step", "count"]).to_csv(out / "funnel_final.csv", index=False)
    print(gv[["dao_id", "governor", "family", "n_decided", "tally_match", "outcome_match",
              "uncovered_blocks", "generic_rule_reproduces"]].to_string(index=False))
    print()
    print(pd.DataFrame(funnel, columns=["step", "count"]).to_string(index=False))
