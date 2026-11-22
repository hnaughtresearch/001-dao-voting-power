# H Naught Research 001 — data collection

Collects on-chain governance data for *How Concentrated Is DAO Voting Power?* following the
research design (Section 3: frame → filters → Tier A / Tier B, and Amendment 1). Nothing here
computes concentration metrics; that comes only after the validation gate passes.

**No paid service is needed.** Logs come from [Envio HyperSync](https://docs.envio.dev/docs/HyperSync/overview)
(free token, no card). Contract state is read at the latest block through ordinary public RPC
endpoints (no archive node).

## Setup (Windows PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH = "src"
$env:HYPERSYNC_TOKEN = "<free token from envio.dev>"
py -m pytest -q
```

Optional: `$env:ETH_RPC_URL`, `$env:ARB_RPC_URL`, `$env:OP_RPC_URL` to use your own RPC
instead of the public defaults in `config.yaml`.

## Run, in order

```powershell
py -m hn scan                 # every governor-family event on Ethereum, Arbitrum One, OP Mainnet
py -m hn select               # frame summary, prefilters, group governors into DAOs by voting token
py -m hn collect --dao ethereum:0x1f9840a85d5af5bf1d1762f925bdaddc4201f984   # trial on Uniswap
py -m hn validate             # check the trial: tally_match and outcome_match must be n/n
py -m hn collect              # all DAOs passing the prefilters
py -m hn validate             # validation gate, exact filters, Tier A / B, funnel
```

Every step is resumable: re-running continues where it stopped. `--chains ethereum` limits a step
to one chain.

## What comes out

| Path | Content |
| --- | --- |
| `data/raw/hypersync/<chain>/events/part-*.jsonl.gz` | Every governor-family log, as received; never edited |
| `data/raw/hypersync/<chain>/proposal_blocks.jsonl.gz` | Timestamp of every `ProposalCreated` block |
| `data/raw/hypersync/<chain>/manifest.json` | Scan height, covered block ranges, SHA-256 of every page |
| `data/processed/frame_<chain>.parquet` | Per contract: proposals before the cutoff, votes, executions |
| `data/processed/funnel_prefilter.csv` | Counts after each prefilter |
| `data/processed/nonstandard_vote_contracts.csv` | Votes but no standard `ProposalCreated` → Tier B review |
| `data/processed/unresolved_token.csv` | Token getter unknown → fill `config/token_overrides.csv` |
| `data/interim/<chain>/<governor>/` | That governor's logs, contract reads, manifest |
| `data/processed/validation_governors.csv` | Tally match, outcome match, gaps, duplicates per governor |
| `data/processed/daos_final.csv`, `funnel_final.csv` | Exact filters and the Tier A / Tier B assignment |

## Design choices (see Amendment 1 in `research_design.md`)

- **Cutoff:** a proposal is in the study if it was created before `cutoff_utc`. The scan runs to the
  HyperSync height at scan time, so votes cast after the cutoff on those proposals are included and
  summed logs can be compared with the tallies stored in the contract.
- **Latest-block reads:** Bravo and OZ governors keep final tallies permanently, and OZ
  `quorum(timepoint)` reads historical checkpoints. Proposals still Pending/Active at read time are
  excluded. A proposal cancelled after the cutoff is excluded as Canceled.
- **Evidence for the source:** `pilot/hypersync_pilot.py` and `pilot/pilot_report.json` show HyperSync
  logs reproducing stored tallies exactly for Uniswap, Arbitrum Core and the Optimism governor, and
  complete history back to Compound Governor Alpha (2020).

## The validation gate (Gate B)

A governor is reproduced by the generic rule only if, for **every** decided proposal in the window:

1. the summed vote weights per side equal the contract's stored tallies exactly;
2. the generic rule (Bravo: For > Against and For ≥ `quorumVotes()`; OZ: rule from
   `COUNTING_MODE()`, quorum from `quorum(snapshot)`) predicts the contract's `state()`;
3. no voter appears twice on a proposal, and the scanned block ranges have no gaps.

A DAO is Tier A only if all its governors pass. Everything else goes to Tier B, with the reason in
a CSV. Nothing is dropped silently.

## Known limits of this version

- Only the Governor family (Compound Alpha/Bravo, OpenZeppelin) is decoded. Aragon (Lido, Curve),
  Aave and Optimism's non-standard proposal modules are Tier B work, not included yet.
- Fractional or module votes (`VoteCastWithParams` with params) are not split; such proposals fail
  the tally check and route the DAO to Tier B, by design.
- Bravo `quorumVotes()` is read at the latest block. If a Bravo DAO changed its quorum over time,
  the outcome check fails and flags it.
