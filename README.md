# H Naught Research 001 — data collection

Collects on-chain governance data for *How Concentrated Is DAO Voting Power?* following the
research design (Section 3: frame → filters → Tier A / Tier B). Nothing here computes
concentration metrics; that comes after the validation gate passes.

## What you need

- Python 3.11+ and `pip install -r requirements.txt`
- A Google Cloud project with BigQuery enabled, authenticated locally
  (`gcloud auth application-default login`). Only step 1 uses it; it is billed per TB scanned.
- **Archive** RPC endpoints, exported as environment variables:
  `ETH_RPC_URL`, `ARB_RPC_URL`, `OP_RPC_URL`. Archive is required because contract state
  (`state()`, tallies, quorum) is read at the fixed cutoff block, not at "latest".

Set the BigQuery billing project with `export HN_BQ_PROJECT=<id>` (not in `config.yaml`, so it never reaches git). The cutoff, filters and chain list are part of
the pre-registration — change them only in a dated commit.

## Run, in order

```bash
export PYTHONPATH=src
python -m hn schema                 # free: confirms the BigQuery logs tables have the expected columns
python -m hn frame                  # free dry run: prints TB scanned and estimated $ per chain
python -m hn frame --execute        # billed: enumerates every contract with governor vote events
python -m hn select                 # RPC: resolve voting token, group governors into DAOs, prefilters
python -m hn collect                # RPC: raw logs + contract reads for DAOs passing prefilters (resumable)
python -m hn validate               # offline: decode, validation gate, exact filters, Tier A/B, funnel
pytest -q                           # offline tests of decoding, rules, chunking and validation
```

`collect` can be limited with `--dao ethereum:0x1f98...` and `--chains ethereum`. It checkpoints
after every log chunk, so an interrupted run continues where it stopped.

## What comes out

| Path | Content |
| --- | --- |
| `data/raw/frame/<chain>.parquet` + `.query.json` | Frame: per-contract event counts; exact SQL, job id, bytes billed |
| `data/raw/<chain>/<governor>/logs.jsonl.gz` | Every raw log as returned by the RPC (never edited) |
| `data/raw/<chain>/<governor>/views.jsonl.gz` | Per proposal: state, tallies, quorum, snapshot, read at the cutoff block |
| `data/raw/<chain>/<governor>/manifest.json` | Covered block ranges, counts, SHA-256 of raw files, RPC host |
| `data/processed/funnel_prefilter.csv` | Counts after each prefilter (BigQuery counts) |
| `data/processed/nonstandard_vote_contracts.csv` | Contracts with votes but no standard `ProposalCreated` → review for Tier B (e.g. Optimism's Agora governor) |
| `data/processed/unresolved_token.csv` | Governors whose token getter is unknown → fill `config/token_overrides.csv` |
| `data/processed/proposals.parquet`, `votes.parquet` | Decoded, de-duplicated tables (weights as strings: uint256) |
| `data/processed/validation_governors.csv` | Per governor: tally match, outcome match, gaps, duplicates |
| `data/processed/daos_final.csv`, `funnel_final.csv` | Exact filters and the Tier A / Tier B assignment |

## The validation gate (Gate B)

A governor counts as reproduced by the generic rule only if, for **every** decided proposal:

1. the sum of decoded vote weights by side equals the contract's stored tallies exactly;
2. the generic rule (Bravo: For > Against and For ≥ `quorumVotes()`; OZ: rule from
   `COUNTING_MODE()`, quorum from `quorum(snapshot)`) predicts the contract's `state()`;
3. no voter appears twice on one proposal, and the logged block ranges have no gaps.

A DAO is Tier A only if all its governors pass. Anything else goes to Tier B for a
hand-written rule. Nothing is dropped silently: every exclusion is in a CSV with its reason.

## Known limits of this version

- Only the Governor family (Compound Alpha/Bravo, OpenZeppelin) is decoded. Aragon (Lido, Curve)
  and Aave decoders are Tier B work and not included yet.
- Fractional or module votes (`VoteCastWithParams` with params, support 255) are not split; such
  proposals fail the tally check and route the DAO to Tier B, by design.
- Bravo `quorumVotes()` is read at the cutoff block. If a Bravo DAO changed its quorum over time,
  the outcome check fails and flags it — then read quorum at each proposal's end block.
- On Arbitrum, governor `startBlock`/`endBlock` values are **L1** block numbers (Arbitrum's
  `block.number`). Timestamps here come from the L2 block of each log, which is correct.
- BigQuery Arbitrum/Optimism datasets are in Preview; every included DAO is re-extracted by RPC,
  so the frame only decides *what* to collect, never the data used in analysis.
- Column names in `sql/frame_governors.sql` follow Google's Blockchain Analytics schema; run
  `hn schema` before the first billed query.
