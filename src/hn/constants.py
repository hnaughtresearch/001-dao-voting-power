"""Event signatures, topics and function selectors used across the pipeline.

Topics are computed from the canonical signatures at import time, so they can be
checked independently (keccak-256 of the signature string).
"""
from eth_hash.auto import keccak


def _topic(sig: str) -> str:
    return "0x" + keccak(sig.encode()).hex()


def selector(sig: str) -> str:
    return "0x" + keccak(sig.encode()).hex()[:8]


# Governor family (Compound Alpha / Bravo and OpenZeppelin Governor).
# Bravo and OZ deliberately share the ProposalCreated and VoteCast signatures.
SIG = {
    "ProposalCreated": "ProposalCreated(uint256,address,address[],uint256[],string[],bytes[],uint256,uint256,string)",
    "VoteCast": "VoteCast(address,uint256,uint8,uint256,string)",            # Bravo + OZ, voter indexed
    "VoteCastWithParams": "VoteCastWithParams(address,uint256,uint8,uint256,string,bytes)",  # OZ, voter indexed
    "VoteCastAlpha": "VoteCast(address,uint256,bool,uint256)",               # Compound-style Alpha, nothing indexed
    "ProposalExecuted": "ProposalExecuted(uint256)",
    "ProposalCanceled": "ProposalCanceled(uint256)",
    "ProposalQueued": "ProposalQueued(uint256,uint256)",
}
TOPIC = {name: _topic(sig) for name, sig in SIG.items()}
TOPIC_TO_NAME = {v: k for k, v in TOPIC.items()}
VOTE_TOPICS = [TOPIC["VoteCast"], TOPIC["VoteCastWithParams"], TOPIC["VoteCastAlpha"]]
ALL_TOPICS = list(TOPIC.values())

# Proposal states: identical enum order in Bravo and OZ Governor.
STATE = {0: "Pending", 1: "Active", 2: "Canceled", 3: "Defeated", 4: "Succeeded",
         5: "Queued", 6: "Expired", 7: "Executed",
         8: "Vetoed", 9: "ObjectionPeriod", 10: "Updatable"}   # 8-10: Nouns V2/V3 only
PASSED_STATES = {"Succeeded", "Queued", "Expired", "Executed"}
DECIDED_STATES = PASSED_STATES | {"Defeated"}

# Support codes in Bravo/OZ simple counting.
AGAINST, FOR, ABSTAIN = 0, 1, 2

FN = {
    "state": selector("state(uint256)"),
    "proposalVotes": selector("proposalVotes(uint256)"),      # OZ: (against, for, abstain)
    "proposals": selector("proposals(uint256)"),              # Bravo/Alpha struct
    "quorumVotes": selector("quorumVotes()"),                 # Bravo/Alpha
    "quorum": selector("quorum(uint256)"),                    # OZ: quorum(timepoint)
    "COUNTING_MODE": selector("COUNTING_MODE()"),             # OZ
    "proposalSnapshot": selector("proposalSnapshot(uint256)"),  # OZ
    "name": selector("name()"),
}
# Voting-token getters, tried in order. Bravo forks name the getter after the token.
TOKEN_GETTERS = ["token()", "comp()", "uni()", "gtc()", "ens()", "pool()", "ampl()"]
