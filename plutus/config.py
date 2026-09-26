"""Paths, logging, and per-token configuration.

DESIGN RULE THIS MODULE EXISTS TO ENFORCE (plan.md non-goals): nothing measured on one token may
become a literal in the engine. A number describing a TOKEN lives in the database keyed by token
(fee, k-stability, quote asset, venues, burn address). A number describing a CHAIN lives in a chain
profile here. the first token onboarded is the first token onboarded, not the subject — `tests/test_no_literals.py`
fails the build on a hardcoded address, supply or fee.
"""
from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TOKENS_DIR = BASE_DIR / "tokens"
# PLUTUS_DB redirects the database: tests use it so they never read or spend the production
# budget, and it lets a second instance run beside the first without sharing state.
DB_PATH = Path(os.environ["PLUTUS_DB"]) if os.environ.get("PLUTUS_DB") else DATA_DIR / "plutus.db"
CACHE_DIR = DATA_DIR / "cache"

DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

_LOG_READY = False


def get_logger(name: str) -> logging.Logger:
    global _LOG_READY
    if not _LOG_READY:
        logging.basicConfig(
            level=os.environ.get("PLUTUS_LOG", "INFO").upper(),
            format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
            datefmt="%H:%M:%S",
        )
        _LOG_READY = True
    return logging.getLogger(name)


# ── chain profiles ────────────────────────────────────────────────────────────
# Facts about a CHAIN, not a token. `addr_lower` is the one that has already cost real debugging
# time elsewhere: Solana addresses are base58 and CASE-SENSITIVE — lowercasing them 404s every
# lookup and the whole lane looks dead rather than broken.
@dataclass(frozen=True)
class ChainProfile:
    name: str
    gecko_network: str
    addr_lower: bool          # EVM: normalize to lowercase. Solana: never.
    native_symbol: str
    verified: bool            # have we actually run the full pipeline here?
    etherscan_chain: int | None = None   # Etherscan v2 chain id (optional, paid on some chains)
    # Free JSON-RPC for the transfer ledger. Only endpoints MEASURED to serve the full history
    # are listed; add others per deployment with PLUTUS_RPC_<CHAIN>=url1,url2.
    rpc_urls: tuple[str, ...] = ()
    confirm_blocks: int = 12              # sync this far behind the head (reorg / lagging nodes)
    time_anchor_blocks: int = 300         # backfill block times: one exact fetch per this many


CHAINS: dict[str, ChainProfile] = {
    "robinhood": ChainProfile("robinhood", "robinhood", True, "ETH", verified=True,
                              etherscan_chain=4663,
                              rpc_urls=("https://rpc.mainnet.chain.robinhood.com",),
                              confirm_blocks=100,       # 0.1s blocks: 10 seconds
                              time_anchor_blocks=36_000),  # one an hour: measured error <= 1s
    "bsc": ChainProfile("bsc", "bsc", True, "BNB", verified=False, etherscan_chain=56),
    "base": ChainProfile("base", "base", True, "ETH", verified=False, etherscan_chain=8453),
    "eth": ChainProfile("eth", "eth", True, "ETH", verified=False, etherscan_chain=1),
    "sol": ChainProfile("sol", "solana", False, "SOL", verified=False),
}


def chain(name: str) -> ChainProfile:
    try:
        return CHAINS[name]
    except KeyError:
        raise ValueError(f"unknown chain {name!r}; known: {', '.join(CHAINS)}") from None


# Characters a spreadsheet, a browser or a chat client will happily paste between addresses and
# that are invisible on screen: BOM, zero-width space/non-joiner/joiner, word joiner, and the
# non-breaking space. Left in, they make a 42-character address 43 characters long and every
# length check fails silently.
_INVISIBLE = dict.fromkeys(map(ord, "﻿​‌‍⁠ "), " ")

_EVM_RE = re.compile(r"0[xX][0-9a-fA-F]{40}")
# base58 alphabet: no 0, O, I or l
_B58_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
_WRAPPERS = chr(34) + chr(39) + "`" + " 	"     # quotes a spreadsheet adds around a cell


def _clean(value: str) -> str:
    """Strip invisible characters and whatever a paste wrapped the address in."""
    return (value or "").translate(_INVISIBLE).strip().strip(_WRAPPERS)


def extract_addresses(chain_name: str, text: str) -> list[str]:
    """Pull every address out of whatever was pasted, in order, de-duplicated.

    EXTRACTION, NOT SPLITTING. Splitting on delimiters assumes you know what the delimiters are,
    and a paste out of Excel does not cooperate: cells arrive wrapped in double quotes whenever
    they contain anything special (which yielded ZERO addresses), separated by tabs, carrying a
    BOM or a zero-width space, sometimes with a capitalised `0X` prefix. Searching for the shape
    of an address instead makes all of that irrelevant — the bytes between matches simply do not
    matter.
    """
    cleaned = (text or "").translate(_INVISIBLE)
    rx = _EVM_RE if chain(chain_name).addr_lower else _B58_RE
    seen: set[str] = set()
    out: list[str] = []
    for m in rx.finditer(cleaned):
        a = norm_addr(chain_name, m.group(0))
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def is_address(chain_name: str, value: str) -> bool:
    """Is this a wallet/contract address, as opposed to a venue identifier?

    Uniswap v4 identifies a pool by a 32-byte hash (66 chars with 0x), and GeckoTerminal returns
    that as the pool's "address". It is not one: nothing holds a balance at a pool ID, and asking
    the vendor for one is rejected outright. Anything that must be balance-queryable goes through
    this check first.
    """
    v = _clean(value)
    if chain(chain_name).addr_lower:
        return bool(_EVM_RE.fullmatch(v))
    return bool(_B58_RE.fullmatch(v))


def norm_addr(chain_name: str, address: str) -> str:
    """Normalize an address for storage and comparison, per chain rules.

    Solana addresses are base58 and CASE-SENSITIVE — lowercasing them 404s every lookup and the
    whole lane looks dead rather than broken. EVM is lowercased so a checksummed paste and a
    lowercase one are the same key.
    """
    a = _clean(address)
    return a.lower() if chain(chain_name).addr_lower else a


# ── token configuration ───────────────────────────────────────────────────────
@dataclass
class TokenConfig:
    """What the operator supplies. Everything else is discovered (see onboard.py).

    `pools` is deliberately NOT required — venue discovery finds them. It exists only so the
    operator can correct a wrong classification.
    """
    chain: str
    address: str
    label: str = ""
    our_wallets: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)   # address -> class (locked/burnt/...)
    pools: list[str] = field(default_factory=list)           # optional override

    @property
    def key(self) -> str:
        return f"{self.chain}:{self.address}"


def load_token(path: str | Path) -> TokenConfig:
    p = Path(path)
    if not p.exists():
        p = TOKENS_DIR / f"{path}.toml"
    with open(p, "rb") as fh:
        raw = tomllib.load(fh)
    tok, ours = raw.get("token", {}), raw.get("ours", {})
    ch = tok["chain"]

    wallets: list[str] = [norm_addr(ch, w) for w in ours.get("wallets", [])]
    if wfile := ours.get("wallets_file"):
        wp = Path(wfile)
        if not wp.is_absolute():
            wp = BASE_DIR / wp
        if wp.exists():
            for line in wp.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    wallets.append(norm_addr(ch, line))

    return TokenConfig(
        chain=ch,
        address=norm_addr(ch, tok["address"]),
        label=tok.get("label", ""),
        our_wallets=sorted(set(wallets)),
        excluded={norm_addr(ch, k): v for k, v in (raw.get("excluded") or {}).items()},
        pools=[norm_addr(ch, p) for p in (raw.get("venues") or {}).get("pools", [])],
    )


def list_tokens() -> list[Path]:
    return sorted(TOKENS_DIR.glob("*.toml"))
