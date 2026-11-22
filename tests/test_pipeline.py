"""Offline tests: no network, no BigQuery. Run with `pytest -q`."""
import gzip
import json

import pytest
from eth_abi import encode

from hn import validate
from hn.constants import TOPIC
from hn.decode import decode_log
from hn.outcome import passes, quorum_basis, flip_mechanism
from hn.rpc import Rpc, RpcError

GOV = "0x" + "ab" * 20
VOTER = "0x" + "cd" * 20


def _log(topic0, data: bytes, topics_extra=(), block=100, idx=0, tx="0x01"):
    return {"address": GOV, "topics": [topic0, *topics_extra], "data": "0x" + data.hex(),
            "blockNumber": hex(block), "transactionHash": tx, "logIndex": hex(idx)}


def _voter_topic(addr):
    return "0x" + "00" * 12 + addr[2:]


def created(pid, start=10, end=20, desc="Test proposal"):
    data = encode(["uint256", "address", "address[]", "uint256[]", "string[]", "bytes[]",
                   "uint256", "uint256", "string"],
                  [pid, VOTER, [VOTER], [0], [""], [b""], start, end, desc])
    return _log(TOPIC["ProposalCreated"], data, idx=pid)


def vote(pid, support, weight, voter=VOTER, idx=0, tx="0x02"):
    data = encode(["uint256", "uint8", "uint256", "string"], [pid, support, weight, "why"])
    return _log(TOPIC["VoteCast"], data, [_voter_topic(voter)], idx=idx, tx=tx)


# --- decoding -------------------------------------------------------------------
def test_decode_created_and_vote():
    p = decode_log(created(7))
    assert p["event"] == "ProposalCreated" and p["proposal_id"] == "7" and p["end_block"] == 20
    v = decode_log(vote(7, 1, 10**24))
    assert v["voter"] == VOTER and v["support"] == 1 and int(v["weight"]) == 10**24


def test_decode_alpha_vote_and_params():
    data = encode(["address", "uint256", "bool", "uint256"], [VOTER, 3, False, 5])
    a = decode_log(_log(TOPIC["VoteCastAlpha"], data))
    assert a["voter"] == VOTER and a["support"] == 0 and a["proposal_id"] == "3"
    data = encode(["uint256", "uint8", "uint256", "string", "bytes"], [3, 255, 9, "", b"\x01"])
    w = decode_log(_log(TOPIC["VoteCastWithParams"], data, [_voter_topic(VOTER)]))
    assert w["has_params"] is True and w["support"] == 255


def test_decode_invalid_utf8_description():
    good = created(1, desc="x")
    raw = bytes.fromhex(good["data"][2:])
    bad = raw.replace(b"x", b"\xff", 1)  # last string payload byte
    good["data"] = "0x" + bad.hex()
    assert decode_log(good)["proposal_id"] == "1"


def test_select_pid_voter_offsets_match_abi_layout():
    """select._pid_and_voter slices raw hex without full ABI decoding."""
    from hn.select import _pid_and_voter
    data = "0x" + encode(["address", "uint256", "bool", "uint256"], [VOTER, 42, True, 1]).hex()
    assert _pid_and_voter({"data": data, "topics": [TOPIC["VoteCastAlpha"]]}, "VoteCastAlpha") == ("42", VOTER)
    d2 = "0x" + encode(["uint256", "uint8", "uint256", "string"], [42, 1, 1, ""]).hex()
    assert _pid_and_voter({"data": d2, "topics": [TOPIC["VoteCast"], _voter_topic(VOTER)]}, "VoteCast") == ("42", VOTER)


# --- outcome rules --------------------------------------------------------------
def test_quorum_basis_parsing():
    assert quorum_basis("oz", "support=bravo&quorum=for,abstain") == ("for", "abstain")
    assert quorum_basis("oz", "support=bravo&quorum=for,abstain&params=fractional") == ("for", "abstain")
    assert quorum_basis("oz", "support=approval&quorum=for") is None      # non-standard -> Tier B
    assert quorum_basis("bravo", None) == ("for",)


def test_passes_and_mechanism():
    t = {"for": 50, "against": 10, "abstain": 0}
    assert passes(t, 40, ("for",), "bravo")
    t2 = {"for": 20, "against": 10, "abstain": 25}
    assert not passes(t2, 40, ("for",), "bravo")                 # Bravo: abstain does not count
    assert passes(t2, 40, ("for", "abstain"), "oz")              # OZ for+abstain
    assert flip_mechanism({"for": 20, "against": 10, "abstain": 0}, 40, ("for",)) == "quorum"
    assert flip_mechanism({"for": 50, "against": 60, "abstain": 0}, 40, ("for",)) == "majority"


# --- RPC chunking ---------------------------------------------------------------
def test_get_logs_halves_on_range_error(monkeypatch):
    rpc = Rpc("http://example.invalid")
    calls = []

    def fake_call(method, params):
        lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        calls.append((lo, hi))
        if hi - lo + 1 > 25:
            raise RpcError("-32005: query returned more than 10000 results")
        return []

    monkeypatch.setattr(rpc, "call", fake_call)
    chunks = list(rpc.get_logs(GOV, [TOPIC["VoteCast"]], 0, 99, chunk=100))
    covered = sorted((lo, hi) for lo, hi, _ in chunks)
    assert covered[0][0] == 0 and covered[-1][1] == 99
    assert all(b[0] == a[1] + 1 for a, b in zip(covered, covered[1:]))   # contiguous, no gaps


def test_gaps():
    assert validate._gaps([[0, 9], [10, 19]], 0, 19) == 0
    assert validate._gaps([[0, 9], [15, 19]], 0, 19) == 5
    assert validate._gaps([[0, 9]], 0, 19) == 10


# --- end-to-end validate on synthetic raw data ---------------------------------
def test_validate_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(validate, "ROOT", tmp_path)
    d = tmp_path / "data" / "interim" / "ethereum" / GOV
    d.mkdir(parents=True)
    (tmp_path / "data" / "processed").mkdir(parents=True)
    v2 = "0x" + "ee" * 20
    logs = [created(1), vote(1, 1, 30, VOTER, 1, "0x10"), vote(1, 0, 10, v2, 2, "0x11"),
            created(2), vote(2, 1, 5, VOTER, 3, "0x12")]
    logs.append(logs[1])                                 # duplicated raw line (resume artefact)
    with gzip.open(d / "logs.jsonl.gz", "wt") as fh:
        for lg in logs:
            fh.write(json.dumps(lg) + "\n")
    views = [
        {"proposal_id": "1", "state": "Executed", "tallies": {"for": "30", "against": "10", "abstain": "0"},
         "quorum": "25", "created_block_ts": 1_600_000_000},
        {"proposal_id": "2", "state": "Defeated", "tallies": {"for": "5", "against": "0", "abstain": "0"},
         "quorum": "25", "created_block_ts": 1_640_000_000},
    ]
    with gzip.open(d / "views.jsonl.gz", "wt") as fh:
        for v in views:
            fh.write(json.dumps(v) + "\n")
    (d / "manifest.json").write_text(json.dumps({
        "chain": "ethereum", "governor": GOV, "start_block": 0, "cutoff_block": 200,
        "ranges": [[0, 200]], "family": "bravo", "counting_mode": None, "token": "0x" + "11" * 20}))

    cfg = {"filters": {"min_decided": 2, "min_median_voters": 1, "min_span_days": 365}}
    validate.run(cfg)
    gv = __import__("pandas").read_csv(tmp_path / "data" / "processed" / "validation_governors.csv")
    row = gv.iloc[0]
    assert row.tally_match == "2/2" and row.outcome_match == "2/2"
    assert bool(row.generic_rule_reproduces) is True
    daos = __import__("pandas").read_csv(tmp_path / "data" / "processed" / "daos_final.csv")
    assert daos.iloc[0].tier == "A"


# --- HyperSync scan output -> select / collect --------------------------------
def _write_scan(root, chain, logs, ts):
    d = root / "data" / "raw" / "hypersync" / chain
    (d / "events").mkdir(parents=True)
    with gzip.open(d / "events" / "part-000000000000-000000000300.jsonl.gz", "wt") as fh:
        for lg in logs:
            fh.write(json.dumps(lg) + "\n")
    with gzip.open(d / "proposal_blocks.jsonl.gz", "wt") as fh:
        for b, t in ts.items():
            fh.write(json.dumps({"block": b, "timestamp": t}) + "\n")
    (d / "manifest.json").write_text(json.dumps({"chain": chain, "source": "test", "scan_height": 301,
                                                 "ranges": [[0, 301]], "n_logs": len(logs)}))


def test_to_rpc_shape_drops_empty_topics():
    from hn.hs import to_rpc_shape
    class L: pass
    lg = L(); lg.address = GOV; lg.topics = [TOPIC["VoteCast"], _voter_topic(VOTER), None, None]
    lg.data = "0x"; lg.block_number = 7; lg.transaction_hash = "0x01"; lg.log_index = 3
    r = to_rpc_shape(lg)
    assert r["topics"] == [TOPIC["VoteCast"], _voter_topic(VOTER)] and r["blockNumber"] == "0x7"


def test_select_summary_respects_cutoff(tmp_path, monkeypatch):
    from hn import hs, select
    monkeypatch.setattr(hs, "ROOT", tmp_path)
    p1, p2 = created(1), created(2)
    p1["blockNumber"], p2["blockNumber"] = hex(100), hex(200)
    logs = [p1, vote(1, 1, 5, VOTER, 1, "0x10"), p2, vote(2, 1, 5, VOTER, 2, "0x11")]
    _write_scan(tmp_path, "ethereum", logs, {100: 1_000, 200: 3_000})
    df = select.summarise_chain("ethereum", cutoff_ts=2_000, rpc=None)
    row = df[df.addr == GOV].iloc[0]
    assert row.n_created_std == 1            # proposal 2 created after the cutoff is excluded
    assert row.median_votes_per_proposal == 1


def test_collect_split_writes_complete_files(tmp_path, monkeypatch):
    from hn import hs, collect
    monkeypatch.setattr(hs, "ROOT", tmp_path)
    monkeypatch.setattr(collect, "ROOT", tmp_path)
    other = dict(created(9)); other["address"] = "0x" + "99" * 20
    _write_scan(tmp_path, "ethereum", [created(1), vote(1, 1, 5), other], {})
    collect._split("ethereum", {GOV})
    f = tmp_path / "data" / "interim" / "ethereum" / GOV / "logs.jsonl.gz"
    lines = gzip.open(f, "rt").read().splitlines()
    assert f.exists() and not f.with_suffix(".gz.part").exists() and len(lines) == 2


def test_indexed_id_variants_do_not_crash():
    from hn.select import _word0_or_topic1
    pid_topic = "0x" + format(77, "064x")
    lg = {"address": GOV, "topics": [TOPIC["ProposalExecuted"], pid_topic], "data": "0x",
          "blockNumber": "0x1", "transactionHash": "0x01", "logIndex": "0x0"}
    assert _word0_or_topic1(lg) == "77"
    assert decode_log(lg)["proposal_id"] == "77"
    bad_vote = {"address": GOV, "topics": [TOPIC["VoteCast"]], "data": "0x1234",
                "blockNumber": "0x1", "transactionHash": "0x02", "logIndex": "0x1"}
    assert decode_log(bad_vote)["event"] == "VoteCast_unparsed"


def test_select_counts_unparsed(tmp_path, monkeypatch):
    from hn import hs, select
    monkeypatch.setattr(hs, "ROOT", tmp_path)
    bad = {"address": GOV, "topics": [TOPIC["VoteCast"]], "data": "0x",
           "blockNumber": "0x5", "transactionHash": "0x03", "logIndex": "0x2"}
    p1 = created(1); p1["blockNumber"] = hex(100)
    _write_scan(tmp_path, "ethereum", [p1, bad], {100: 1_000})
    df = select.summarise_chain("ethereum", cutoff_ts=2_000, rpc=None)
    assert df[df.addr == GOV].iloc[0].n_unparsed == 1


def test_resolve_verify_matches_vote_weight():
    from hn import resolve
    class FakeRpc: pass
    rpc = FakeRpc()
    props = {"1": {"start_block": 50, "block": 40}}
    votes = [{"proposal_id": "1", "voter": VOTER, "weight": "30"}]
    resolve._votes_at = lambda r, tok, voter, block: 30 if block == 50 else 0
    assert resolve._verify(rpc, GOV, "0x" + "11" * 20, "bravo", props, votes) == (1, 1)
    resolve._votes_at = lambda r, tok, voter, block: 29
    assert resolve._verify(rpc, GOV, "0x" + "11" * 20, "bravo", props, votes) == (0, 1)
    assert resolve._addr_topic(VOTER) == _voter_topic(VOTER)
