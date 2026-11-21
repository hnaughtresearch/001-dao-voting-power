-- H Naught 001 · Frame enumeration: every contract that emitted governor vote events.
-- Parameters: @cutoff (TIMESTAMP), @t_created, @t_exec, @t_cancel, @t_vote, @t_vote_params, @t_vote_alpha (STRING).
-- {table} is substituted per chain from config.yaml.
-- Column names follow the Google Blockchain Analytics logs schema (address, topics, data,
-- block_number, block_timestamp). Run `hn schema` first to confirm them.

WITH ev AS (
  SELECT
    LOWER(address) AS addr,
    LOWER(topics[SAFE_OFFSET(0)]) AS t0,
    LOWER(topics[SAFE_OFFSET(1)]) AS t1,
    LOWER(data) AS data,
    block_number,
    block_timestamp
  FROM `{table}`
  WHERE block_timestamp < @cutoff
    AND LOWER(topics[SAFE_OFFSET(0)]) IN UNNEST([@t_created, @t_exec, @t_cancel, @t_vote, @t_vote_params, @t_vote_alpha])
),
votes AS (
  SELECT
    addr,
    -- proposalId: first data word for Bravo/OZ, second for Alpha (voter not indexed)
    IF(t0 = @t_vote_alpha, SUBSTR(data, 67, 64), SUBSTR(data, 3, 64)) AS pid,
    IF(t0 = @t_vote_alpha, CONCAT('0x', SUBSTR(data, 27, 40)), CONCAT('0x', SUBSTR(t1, 27, 40))) AS voter,
    t0
  FROM ev
  WHERE t0 IN (@t_vote, @t_vote_params, @t_vote_alpha)
),
per_proposal AS (
  SELECT addr, pid, COUNT(DISTINCT voter) AS n_voters, COUNTIF(t0 = @t_vote_params) AS n_param_votes
  FROM votes GROUP BY addr, pid
),
per_gov AS (
  SELECT
    addr,
    COUNTIF(t0 = @t_created) AS n_created_std,
    COUNTIF(t0 = @t_exec) AS n_executed,
    COUNTIF(t0 = @t_cancel) AS n_canceled,
    COUNTIF(t0 IN (@t_vote, @t_vote_params, @t_vote_alpha)) AS n_votes,
    MIN(block_number) AS first_block,
    MAX(block_number) AS last_block,
    MIN(block_timestamp) AS first_ts,
    MAX(block_timestamp) AS last_ts
  FROM ev GROUP BY addr
)
SELECT
  g.*,
  COUNT(p.pid) AS n_proposals_with_votes,
  APPROX_QUANTILES(p.n_voters, 2)[OFFSET(1)] AS median_voters,
  SUM(p.n_param_votes) AS n_param_votes
FROM per_gov g
LEFT JOIN per_proposal p USING (addr)
WHERE g.n_votes > 0
GROUP BY g.addr, g.n_created_std, g.n_executed, g.n_canceled, g.n_votes,
         g.first_block, g.last_block, g.first_ts, g.last_ts
