"""Minimal JSON-RPC client with retries and adaptive eth_getLogs chunking."""
from __future__ import annotations

import time
from typing import Any, Iterator

import requests

# Substrings that providers use when a log range or result set is too large.
_RANGE_ERRORS = ("range", "too many", "more than", "exceed", "limit", "query returned",
                 "response size", "timeout", "-32005", "-32602")


class RpcError(RuntimeError):
    pass


class Rpc:
    def __init__(self, url: str, max_retries: int = 6, timeout: int = 60, min_interval: float = 0.0):
        if not url:
            raise ValueError("RPC URL is empty: set the environment variable named in config.yaml")
        self.url = url
        self.max_retries = max_retries
        self.timeout = timeout
        self.min_interval = min_interval  # seconds between calls, for rate-limited free tiers
        self._last = 0.0
        self._id = 0
        self.session = requests.Session()

    def call(self, method: str, params: list) -> Any:
        delay = 1.0
        for attempt in range(self.max_retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._id += 1
            try:
                r = self.session.post(self.url, json={"jsonrpc": "2.0", "id": self._id,
                                                      "method": method, "params": params},
                                      timeout=self.timeout)
                self._last = time.monotonic()
                if r.status_code == 429 or r.status_code >= 500:
                    raise RpcError(f"HTTP {r.status_code}")
                body = r.json()
            except (requests.RequestException, ValueError, RpcError) as e:
                if attempt == self.max_retries - 1:
                    raise RpcError(f"{method} failed after retries: {e}") from e
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if "error" in body:
                raise RpcError(f"{body['error'].get('code')}: {body['error'].get('message')}")
            return body["result"]
        raise RpcError(f"{method}: exhausted retries")

    # --- helpers -------------------------------------------------------------
    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_timestamp(self, n: int) -> int:
        b = self.call("eth_getBlockByNumber", [hex(n), False])
        return int(b["timestamp"], 16)

    def eth_call(self, to: str, data: str, block: int | str = "latest") -> str:
        tag = hex(block) if isinstance(block, int) else block
        return self.call("eth_call", [{"to": to, "data": data}, tag])

    def get_logs(self, address: str, topics0: list[str], start: int, end: int,
                 chunk: int = 50_000) -> Iterator[tuple[int, int, list[dict]]]:
        """Yield (from_block, to_block, logs) chunks, halving the range on provider limits."""
        cur = start
        size = max(1, chunk)
        while cur <= end:
            hi = min(end, cur + size - 1)
            try:
                logs = self.call("eth_getLogs", [{"address": address, "fromBlock": hex(cur),
                                                  "toBlock": hex(hi), "topics": [topics0]}])
            except RpcError as e:
                msg = str(e).lower()
                if size > 1 and any(s in msg for s in _RANGE_ERRORS):
                    size = max(1, size // 2)
                    continue
                raise
            yield cur, hi, logs
            cur = hi + 1
            if len(logs) < 1000:          # grow back slowly after a dense stretch
                size = min(chunk, size * 2)


def block_at_or_before(rpc: Rpc, ts: int) -> int:
    """Highest block whose timestamp is <= ts (binary search)."""
    lo, hi = 0, rpc.block_number()
    if rpc.block_timestamp(hi) <= ts:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if rpc.block_timestamp(mid) <= ts:
            lo = mid
        else:
            hi = mid - 1
    return lo
