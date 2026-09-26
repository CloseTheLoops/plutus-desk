"""Where the transfer ledger reads from: the chain's free RPC first, Etherscan only if keyed.

Both serve the same three facts -- the token's Transfer events, its total supply, the chain
head -- so the ledger does not care which. The free RPC is preferred whenever the chain has
one; Etherscan is used only where there is no RPC and a key is present (it needs a paid plan
on some chains, e.g. Robinhood from 2026-10-16). PLUTUS_LEDGER_SOURCE=etherscan|rpc forces one.
"""
from __future__ import annotations

import os

from plutus import config
from plutus.sources import chainrpc, etherscan


class LedgerSourceError(RuntimeError):
    pass


class Source:
    name = ""

    def latest_block(self) -> int: ...
    def decimals(self, token: str) -> int: ...
    def token_supply(self, token: str, block: int | None = None) -> int: ...
    def transfer_logs(self, token: str, lo: int, hi: int, on_chunk=None) -> list[dict]: ...
    def calls(self) -> int: ...


class RpcSource(Source):
    def __init__(self, chain: str):
        self.chain = chain
        self.name = "rpc " + ", ".join(e.host for e in chainrpc.endpoints(chain))

    def latest_block(self):
        return self._w(chainrpc.latest_block, self.chain)

    def decimals(self, token):
        return self._w(chainrpc.decimals, self.chain, token)

    def token_supply(self, token, block=None):
        return self._w(chainrpc.token_supply, self.chain, token, block)

    def transfer_logs(self, token, lo, hi, on_chunk=None):
        try:
            return chainrpc.transfer_logs(self.chain, token, lo, hi, on_chunk=on_chunk)
        except chainrpc.RpcError as exc:
            raise LedgerSourceError(str(exc)) from exc

    def calls(self):
        return chainrpc.calls()

    @staticmethod
    def _w(fn, *a):
        try:
            return fn(*a)
        except chainrpc.RpcError as exc:
            raise LedgerSourceError(str(exc)) from exc


class EtherscanSource(Source):
    def __init__(self, chain: str):
        self.cid = config.chain(chain).etherscan_chain
        self.name = f"etherscan (chainid {self.cid})"

    def latest_block(self):
        return self._w(etherscan.latest_block, self.cid)

    def decimals(self, token):
        return self._w(etherscan.decimals, self.cid, token)

    def token_supply(self, token, block=None):          # Etherscan reads the latest supply only
        return self._w(etherscan.token_supply, self.cid, token)

    def transfer_logs(self, token, lo, hi, on_chunk=None):
        logs = self._w(etherscan.transfer_logs, self.cid, token, lo, hi)
        if on_chunk is None:
            return logs
        on_chunk(logs, hi)
        return []

    def calls(self):
        return etherscan.budget().get("day") or 0

    @staticmethod
    def _w(fn, *a):
        try:
            return fn(*a)
        except etherscan.EtherscanError as exc:          # NoKey, PlanRequired, OverBudget too
            raise LedgerSourceError(str(exc)) from exc


def for_chain(chain: str) -> Source | None:
    force = (os.environ.get("PLUTUS_LEDGER_SOURCE") or "").strip().lower()
    rpc_ok = chainrpc.available(chain)
    try:
        es_ok = etherscan.available(chain)
    except KeyError:
        es_ok = False
    if force == "etherscan":
        return EtherscanSource(chain) if es_ok else None
    if force == "rpc":
        return RpcSource(chain) if rpc_ok else None
    if rpc_ok:
        return RpcSource(chain)
    if es_ok:
        return EtherscanSource(chain)
    return None


def available(chain: str) -> bool:
    return for_chain(chain) is not None
