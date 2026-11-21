"""Step 1 · Frame enumeration in BigQuery (dry-run cost check before any billed query)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .constants import TOPIC

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_COLUMNS = {"address", "topics", "data", "block_number", "block_timestamp"}


def _client(project: str):
    from google.cloud import bigquery  # imported lazily: only this step needs it
    project = os.environ.get("HN_BQ_PROJECT") or project  # env var keeps the project id out of git
    if not project or project.startswith("YOUR_"):
        raise SystemExit("set HN_BQ_PROJECT (or bigquery.project in config.yaml)")
    return bigquery, bigquery.Client(project=project)


def schema_check(cfg: dict) -> None:
    """Free: reads table metadata only."""
    bq, client = _client(cfg["bigquery"]["project"])
    for chain, c in cfg["chains"].items():
        table = client.get_table(c["bq_logs_table"])
        cols = {f.name for f in table.schema}
        missing = REQUIRED_COLUMNS - cols
        print(f"{chain}: {c['bq_logs_table']} rows={table.num_rows} "
              f"size={table.num_bytes / 1e12:.2f} TB missing={sorted(missing) or 'none'}")


def run(cfg: dict, chains: list[str], execute: bool) -> None:
    bq, client = _client(cfg["bigquery"]["project"])
    sql_tpl = (ROOT / "sql" / "frame_governors.sql").read_text()
    cutoff = dt.datetime.fromisoformat(cfg["cutoff_utc"]).replace(tzinfo=dt.timezone.utc)
    cap = int(cfg["bigquery"]["max_bytes_billed"])
    params = [bq.ScalarQueryParameter("cutoff", "TIMESTAMP", cutoff)] + [
        bq.ScalarQueryParameter(name, "STRING", TOPIC[key]) for name, key in [
            ("t_created", "ProposalCreated"), ("t_exec", "ProposalExecuted"),
            ("t_cancel", "ProposalCanceled"), ("t_vote", "VoteCast"),
            ("t_vote_params", "VoteCastWithParams"), ("t_vote_alpha", "VoteCastAlpha")]]
    out_dir = ROOT / "data" / "raw" / "frame"
    out_dir.mkdir(parents=True, exist_ok=True)

    for chain in chains:
        table = cfg["chains"][chain]["bq_logs_table"]
        sql = sql_tpl.replace("{table}", table)
        dry = client.query(sql, job_config=bq.QueryJobConfig(query_parameters=params, dry_run=True,
                                                             use_query_cache=False))
        tb = dry.total_bytes_processed / 1e12
        usd = tb * float(cfg["bigquery"]["usd_per_tb"])
        print(f"[{chain}] dry run: {tb:.3f} TB scanned ≈ ${usd:,.2f} (cap {cap / 1e12:.2f} TB)")
        if not execute:
            continue
        if dry.total_bytes_processed > cap:
            print(f"[{chain}] SKIPPED: above max_bytes_billed. Raise the cap in config.yaml if intended.")
            continue
        job = client.query(sql, job_config=bq.QueryJobConfig(query_parameters=params,
                                                             maximum_bytes_billed=cap))
        df = job.result().to_dataframe()
        df.insert(0, "chain", chain)
        path = out_dir / f"{chain}.parquet"
        df.to_parquet(path, index=False)
        (out_dir / f"{chain}.query.json").write_text(json.dumps({
            "table": table, "cutoff_utc": cfg["cutoff_utc"], "job_id": job.job_id,
            "bytes_billed": job.total_bytes_billed, "rows": len(df),
            "run_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "sql": sql}, indent=2))
        print(f"[{chain}] {len(df)} contracts with vote events → {path}")
