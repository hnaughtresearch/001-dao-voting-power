"""H Naught Research 001 · data collection CLI (no paid services).

  python -m hn scan [--chains ...]    # HyperSync: every governor event on each chain (resumable)
  python -m hn select                 # frame summary, prefilters, group governors into DAOs (RPC)
  python -m hn resolve                # infer + verify tokens of governors with unknown getters, then select again
  python -m hn collect [--dao ID ...] # per-governor logs + contract reads at latest block (RPC)
  python -m hn validate               # decode, validation gate, exact filters, Tier A / B, funnel
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def make_rpcs(cfg: dict, chains: list[str]):
    """RPC per chain: env var if set, else the public endpoint from config.yaml."""
    from .rpc import Rpc
    out = {}
    for c in chains:
        cc = cfg["chains"][c]
        url = os.environ.get(cc["rpc_env"]) or cc["rpc_default"]
        out[c] = Rpc(url, min_interval=float(cfg["rpc"]["min_interval_s"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="hn")
    ap.add_argument("step", choices=["scan", "select", "resolve", "collect", "validate"])
    ap.add_argument("--chains", nargs="*", help="subset of chains from config.yaml")
    ap.add_argument("--dao", nargs="*", help="collect: only these dao_ids (chain:token)")
    a = ap.parse_args()
    cfg = load_cfg()
    chains = a.chains or list(cfg["chains"])
    (ROOT / "data" / "processed").mkdir(parents=True, exist_ok=True)

    if a.step == "scan":
        from . import hs
        hs.run(cfg, chains)
    elif a.step == "select":
        from . import select
        select.run(cfg, make_rpcs(cfg, chains), chains)
    elif a.step == "resolve":
        from . import resolve
        resolve.run(cfg, make_rpcs(cfg, chains), chains)
    elif a.step == "collect":
        from . import collect
        collect.run(cfg, make_rpcs(cfg, chains), a.dao)
    elif a.step == "validate":
        from . import validate
        validate.run(cfg)


if __name__ == "__main__":
    main()
