"""H Naught Research 001 · data collection CLI.

  python -m hn schema                 # free: check BigQuery table columns
  python -m hn frame                  # dry run: print TB scanned and estimated cost per chain
  python -m hn frame --execute        # run the billed enumeration (respects max_bytes_billed)
  python -m hn select                 # group governors into DAOs, apply prefilters (RPC)
  python -m hn collect [--dao ID ...] # extract logs + contract reads (RPC, resumable)
  python -m hn validate               # decode, validation gate, exact filters, tiers
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def make_rpcs(cfg: dict, chains: list[str]):
    from .rpc import Rpc
    out = {}
    for c in chains:
        url = os.environ.get(cfg["chains"][c]["rpc_env"], "")
        if not url:
            print(f"skip {c}: env {cfg['chains'][c]['rpc_env']} not set", file=sys.stderr)
            continue
        out[c] = Rpc(url, min_interval=float(cfg["rpc"]["min_interval_s"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="hn")
    ap.add_argument("step", choices=["schema", "frame", "select", "collect", "validate"])
    ap.add_argument("--chains", nargs="*", help="subset of chains from config.yaml")
    ap.add_argument("--execute", action="store_true", help="frame: run the billed query")
    ap.add_argument("--dao", nargs="*", help="collect: only these dao_ids (chain:token)")
    a = ap.parse_args()
    cfg = load_cfg()
    chains = a.chains or list(cfg["chains"])
    (ROOT / "data" / "processed").mkdir(parents=True, exist_ok=True)

    if a.step == "schema":
        from . import frame
        frame.schema_check(cfg)
    elif a.step == "frame":
        from . import frame
        frame.run(cfg, chains, a.execute)
    elif a.step == "select":
        from . import select
        select.run(cfg, make_rpcs(cfg, chains), chains)
    elif a.step == "collect":
        from . import collect
        collect.run(cfg, make_rpcs(cfg, chains), a.dao)
    elif a.step == "validate":
        from . import validate
        validate.run(cfg)


if __name__ == "__main__":
    main()
