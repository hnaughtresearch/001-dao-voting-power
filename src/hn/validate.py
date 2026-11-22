"""Step 4 · Decode per-governor data, verify it against the contracts, build the analysis sets.

Two verification levels, each required on 100% of a governor's decided proposals:
  votes_verified     summed vote weights from the logs equal the tallies the contract stores.
                     The tallies are read from proposalVotes(), from the Bravo proposals() struct,
                     or from the struct position that matches the log sums on every proposal
                     (Bravo forks and Nouns-style governors use other layouts).
  outcome_verified   a counting rule + quorum getter reproduces the contract's vote-stage result.
                     Candidates come from COUNTING_MODE() and the governor family; quorum getters
                     are tried in the family's documented order. If two different rules both fit,
                     the governor is marked ambiguous, not verified.
Governors are excluded (with a reason) if they run elections, or if they are satellites with
negligible participation (median distinct voters per decided proposal < min_median_voters).
DAO sets:  A = outcomes verified (all hypotheses) · B = votes verified only (H1, H3, H4) · excluded.
Outputs in data/processed/: proposals.parquet, votes.parquet, validation_proposals.csv,
validation_governors.csv, excluded_governors.csv, daos_final.csv, funnel_final.csv.
"""
from __future__ import annotations

import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs

import pandas as pd

from .constants import DECIDED_STATES, PASSED_STATES, AGAINST, FOR, ABSTAIN
from .decode import decode_log
from .outcome import quorum_basis, is_election

ROOT = Path(__file__).resolve().parents[2]
SUPPORT_KEY = {AGAINST: "against", FOR: "for", ABSTAIN: "abstain"}
FIELDS = ("for", "against", "abstain")
QUORUM_ORDER = {"oz": ["quorum(snapshot)", "quorum(proposalId)", "quorumVotes(proposalId)", "quorumVotes()"],
                "bravo": ["quorumVotes()", "quorumVotes(proposalId)", "quorum(snapshot)", "quorum(proposalId)"],
                "unknown": ["quorumVotes(proposalId)", "quorumVotes()", "quorum(snapshot)", "quorum(proposalId)"]}


# --- loading -------------------------------------------------------------------------------
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


# --- tallies ---------------------------------------------------------------------------------
def detect_layout(pids: list[str], words: dict[str, list[int]], sums: dict) -> dict | None:
    """Struct positions of for/against/abstain that equal the log sums on every proposal.
    A field that is zero on every proposal (e.g. abstain in Alpha) maps to None. Returns None if
    any field has no position or more than one candidate position."""
    if not pids or any(words.get(p) is None for p in pids):
        return None
    pos = {}
    for f in FIELDS:
        vals = [sums[p][f] for p in pids]
        if not any(vals):
            if f != "abstain":
                return None
            pos[f] = None
            continue
        cand = None
        for p, v in zip(pids, vals):
            hits = {i for i, x in enumerate(words[p]) if x == v}
            cand = hits if cand is None else cand & hits
            if not cand:
                return None
        if len(cand) != 1:
            return None
        pos[f] = cand.pop()
    used = [i for i in pos.values() if i is not None]
    return pos if len(used) == len(set(used)) else None


# --- outcome rules ---------------------------------------------------------------------------
def _rules(fam: str, cm: str | None) -> list[tuple[str, tuple, bool]]:
    """Candidate (name, quorum basis, majority_required) from COUNTING_MODE and family."""
    if fam == "oz" and cm:
        qs = parse_qs(cm)
        if "counting" in qs:
            return []
        basis = quorum_basis(fam, cm)
        if basis is None:                       # e.g. success=…, support=against,for, params=modules
            q = qs.get("quorum", [""])[0]
            basis = ("for",) if q == "bravo" else tuple(x for x in q.split(",") if x) or ("for",)
        success = qs.get("success", [None])[0]
        if success == "quorum":
            return [("quorum_only", basis, False)]
        if success == "majority":
            return [("majority_and_quorum", basis, True), ("majority_only", basis, None)]
        return [("majority_and_quorum", basis, True)]
    return [("majority_and_quorum", ("for",), True)]       # Bravo, Alpha, Nouns-style


def _passes(t: dict, q: int, basis: tuple, majority) -> bool:
    if majority is None:                                     # majority_only
        return t["for"] > t["against"]
    quorum_ok = sum(t[k] for k in basis) >= q
    return quorum_ok and (t["for"] > t["against"] if majority else True)


def find_outcome_rule(fam, cm, decided, qvars, tallies):
    """First rule × quorum getter (in family order) that reproduces every decided outcome.
    Returns (rule_name, quorum_getter, status) with status 'verified', 'ambiguous' or 'none'."""
    order = QUORUM_ORDER.get(fam, QUORUM_ORDER["unknown"])
    fits = []
    for name, basis, maj in _rules(fam, cm):
        for qg in order:
            ok = True
            for pid, st in decided:
                q = qvars.get(pid, {}).get(qg)
                if q is None and maj is not None:
                    ok = False
                    break
                pred = _passes(tallies[pid], q or 0, basis, maj)
                if pred != (st in PASSED_STATES):
                    ok = False
                    break
            if ok:
                fits.append((name, qg, basis, maj))
                break                                       # first getter in documented order
    if not fits:
        return None, None, "none"
    rules = {(n, b, m) for n, _, b, m in fits}
    if len(rules) > 1:
        return fits[0][0], fits[0][1], "ambiguous"
    return fits[0][0], fits[0][1], "verified"


# --- exceptions ------------------------------------------------------------------------------
def load_binding_exceptions() -> list[dict]:
    p = ROOT / "config" / "binding_exceptions.csv"
    if not p.exists():
        return []
    with open(p, newline="") as fh:
        return [r for r in csv.DictReader(fh)]


# --- main ------------------------------------------------------------------------------------
def run(cfg: dict) -> None:
    f = cfg["filters"]
    min_voters = int(f["min_median_voters"])
    out = ROOT / "data" / "processed"
    exceptions = load_binding_exceptions()
    all_p, all_v, prow, grow = [], [], [], []

    for d in sorted((ROOT / "data" / "interim").glob("*/0x*")):
        man, rows, views = _load_governor(d)
        chain, gov = man["chain"], man["governor"]
        fam, cm = man.get("family"), man.get("counting_mode")
        props = [r for r in rows if r["event"] == "ProposalCreated" and r["proposal_id"] in views]
        votes = [r for r in rows if r["event"] in ("VoteCast", "VoteCastWithParams", "VoteCastAlpha")]
        executed_block = {r["proposal_id"]: r["block"] for r in rows if r["event"] == "ProposalExecuted"}
        n_unparsed = sum(1 for r in rows if r["event"].endswith("_unparsed"))

        # Binding exception: proposals created after a documented move of execution off this governor.
        cut_block = None
        for ex in exceptions:
            if ex["chain"] == chain and ex["governor"].lower() == gov and ex["proposal_id"] in executed_block:
                cut_block = executed_block[ex["proposal_id"]]
        n_after_cut = 0
        if cut_block is not None:
            n_after_cut = sum(1 for p in props if p["block"] > cut_block)
            props = [p for p in props if p["block"] <= cut_block]
        in_window = {p["proposal_id"] for p in props}
        votes = [v for v in votes if v["proposal_id"] in in_window]
        for r in props + votes:
            r["chain"] = chain
        all_p += props
        all_v += votes

        sums = defaultdict(lambda: {"for": 0, "against": 0, "abstain": 0})
        pair_count, voters_by_pid, other_support = defaultdict(int), defaultdict(set), defaultdict(int)
        for v in votes:
            pair_count[(v["proposal_id"], v["voter"])] += 1
            voters_by_pid[v["proposal_id"]].add(v["voter"])
            k = SUPPORT_KEY.get(v["support"])
            if k:
                sums[v["proposal_id"]][k] += int(v["weight"])
            else:
                other_support[v["proposal_id"]] += 1
        dup_pids = {pid for (pid, _), n in pair_count.items() if n > 1}

        decided = [(p["proposal_id"], views[p["proposal_id"]].get("state")) for p in props
                   if views[p["proposal_id"]].get("state") in DECIDED_STATES]
        dpids = [pid for pid, _ in decided]
        n_state_unreadable = sum(1 for p in props if views[p["proposal_id"]].get("state") is None)

        # Votes verification: parsed tallies first, then a struct layout that fits every proposal.
        parsed = {pid: {k: int(x) for k, x in views[pid].get("tallies", {}).items()} or None for pid in dpids}
        parsed_ok = bool(dpids) and all(parsed[p] == sums[p] for p in dpids)
        layout = None
        if parsed_ok:
            tally_source = "proposalVotes/proposals"
        else:
            words = {p: [int(w) for w in views[p]["struct_words"]] if views[p].get("struct_words") else None
                     for p in dpids}
            layout = detect_layout(dpids, words, sums)
            tally_source = f"struct_layout {layout}" if layout else None
        votes_ok = (tally_source is not None and not dup_pids and n_unparsed == 0
                    and _gaps(man["ranges"], man["start_block"], man["cutoff_block"]) == 0
                    and not any(other_support[p] for p in dpids))

        # Outcome verification (only meaningful when the tallies are verified).
        rule = qgetter = None
        outcome_status = "none"
        if votes_ok:
            qvars = {p: {k: int(x) for k, x in views[p].get("quorum_variants", {}).items()} for p in dpids}
            rule, qgetter, outcome_status = find_outcome_rule(fam, cm, decided, qvars, {p: sums[p] for p in dpids})
        outcome_ok = votes_ok and outcome_status == "verified"

        med_voters = float(pd.Series([len(voters_by_pid[p]) for p in dpids]).median()) if dpids else 0.0
        kind = "election" if is_election(cm) else "proposals"
        if kind == "election":
            include, why = False, "election governor (no pass/fail outcome)"
        elif med_voters < min_voters:
            include, why = False, f"satellite: median voters {med_voters:g} < {min_voters}"
        else:
            include, why = True, ""

        for p in props:
            pid = p["proposal_id"]
            vw = views[pid]
            prow.append({"chain": chain, "governor": gov, "proposal_id": pid, "state": vw.get("state"),
                         "decided": vw.get("state") in DECIDED_STATES, "n_voters": len(voters_by_pid[pid]),
                         "logs_for": str(sums[pid]["for"]), "logs_against": str(sums[pid]["against"]),
                         "logs_abstain": str(sums[pid]["abstain"]),
                         "contract_tallies": json.dumps(vw.get("tallies")), "quorum_variants": json.dumps(vw.get("quorum_variants")),
                         "created_block": p["block"], "created_ts": vw.get("created_block_ts"),
                         "duplicate_voters": pid in dup_pids})
        grow.append({"chain": chain, "governor": gov, "family": fam, "counting_mode": cm, "kind": kind,
                     "n_proposals": len(props), "n_decided": len(dpids), "n_state_unreadable": n_state_unreadable,
                     "n_excluded_after_binding_exception": n_after_cut,
                     "median_voters_decided": med_voters, "duplicate_pairs": len(dup_pids),
                     "unparsed_logs": n_unparsed, "tally_source": tally_source, "votes_verified": votes_ok,
                     "outcome_rule": rule, "quorum_getter": qgetter, "outcome_status": outcome_status,
                     "outcome_verified": outcome_ok, "included": include, "exclusion_reason": why})

    pd.DataFrame(all_p).to_parquet(out / "proposals.parquet", index=False)
    pd.DataFrame(all_v).to_parquet(out / "votes.parquet", index=False)   # weight as string: uint256
    pv = pd.DataFrame(prow)
    pv.to_csv(out / "validation_proposals.csv", index=False)
    gv = pd.DataFrame(grow)

    cand = ROOT / "data" / "processed" / "dao_candidates.json"
    member = {}
    if cand.exists():
        for d in json.loads(cand.read_text()):
            for g in d["governors"]:
                member[(d["chain"], g.lower())] = d["dao_id"]
    gv["dao_id"] = [member.get((c, g), f"{c}:unknown") for c, g in zip(gv.chain, gv.governor)]
    gv.to_csv(out / "validation_governors.csv", index=False)
    gv[~gv.included].to_csv(out / "excluded_governors.csv", index=False)

    inc = gv[gv.included]
    pv = pv.merge(inc[["chain", "governor", "dao_id"]], on=["chain", "governor"])
    dec = pv[pv.decided]
    dao = dec.groupby("dao_id").agg(n_decided=("proposal_id", "count"), median_voters=("n_voters", "median"),
                                    first_ts=("created_ts", "min"), last_ts=("created_ts", "max"))
    dao["span_days"] = (dao.last_ts - dao.first_ts) / 86400
    dao["n_governors"] = inc.groupby("dao_id").size()
    dao["votes_verified"] = inc.groupby("dao_id").votes_verified.all()
    dao["outcome_verified"] = inc.groupby("dao_id").outcome_verified.all()
    funnel = [("daos_with_included_governors", len(dao))]
    keep = pd.Series(True, index=dao.index)
    for name, cond in [
        (f"decided>={f['min_decided']}", dao.n_decided >= int(f["min_decided"])),
        (f"median_voters>={min_voters}", dao.median_voters >= min_voters),
        (f"span>={f['min_span_days']}d", dao.span_days >= int(f["min_span_days"])),
    ]:
        keep &= cond
        funnel.append((name, int(keep.sum())))
    dao["passes_filters"] = keep
    dao["set"] = "excluded"
    dao.loc[keep & dao.votes_verified, "set"] = "B"
    dao.loc[keep & dao.outcome_verified, "set"] = "A"
    dao.loc[keep & ~dao.votes_verified, "set"] = "unverified"
    funnel += [("set_A_outcomes_verified", int((dao.set == "A").sum())),
               ("set_B_votes_verified_only", int((dao.set == "B").sum())),
               ("unverified (not analysed)", int((dao.set == "unverified").sum()))]
    dao.reset_index().to_csv(out / "daos_final.csv", index=False)
    pd.DataFrame(funnel, columns=["step", "count"]).to_csv(out / "funnel_final.csv", index=False)

    pd.set_option("display.width", 250)
    print(gv[["dao_id", "governor", "kind", "n_decided", "median_voters_decided", "votes_verified",
              "tally_source", "outcome_status", "quorum_getter", "included"]].to_string(index=False))
    print()
    print(dao.reset_index()[["dao_id", "n_governors", "n_decided", "median_voters", "span_days", "set"]].to_string(index=False))
    print()
    print(pd.DataFrame(funnel, columns=["step", "count"]).to_string(index=False))
