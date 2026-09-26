"""Address extraction must survive whatever a spreadsheet produces.

The operator pasted a wallet list out of Excel and got a blank result. The parser was SPLITTING
on delimiters, which assumes you know what the delimiters are; Excel wraps cells in double quotes
whenever they contain anything special, and that yielded ZERO addresses. These are the shapes a
real paste actually arrives in.
"""
from __future__ import annotations

import os as _os_rpc_guard
_os_rpc_guard.environ["PLUTUS_RPC_DISABLE"] = "1"          # never a real chain node here

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plutus import config  # noqa: E402

A = "0xa0b1c2d3e4f5061728394a5b6c7d8e9f01234567"
B = A[:-1] + "1"

SHAPES = {
    "plain newlines": f"{A}\n{B}",
    "CRLF, windows excel": f"{A}\r\n{B}",
    "CR only, old mac": f"{A}\r{B}",
    "tab separated columns": f"{A}\tlabel\n{B}\tlabel2",
    "non-breaking space": f"{A}\xa0\n\xa0{B}",
    "zero-width space": f"{A}\u200b\n{B}",
    "BOM at the start": f"\ufeff{A}\n{B}",
    "quoted cells": f'"{A}"\n"{B}"',
    "capitalised 0X prefix": f"{A.replace('0x', '0X')}\n{B}",
    "one cell, space separated": f"{A} {B}",
    "checksummed mixed case": f"0XA0B1C2D3E4F5061728394A5B6C7D8E9F01234567\n{B}",
    "csv with quoted notes": f'"{A}","note"\n"{B}","note"',
    "no separators at all": f"{A}{B}",
    "with a header row": f"wallet\taddress\n{A}\tmain\n{B}\tsecond",
    "trailing blank lines": f"{A}\n{B}\n\n\n",
    "comments after each": f"{A} # main\n{B} # backup",
}


def test_every_paste_shape_yields_both_addresses():
    for name, text in SHAPES.items():
        got = config.extract_addresses("robinhood", text)
        assert got == [A, B], f"{name}: got {len(got)} -> {got}"


def test_order_is_preserved_and_duplicates_collapse():
    assert config.extract_addresses("robinhood", f"{B}\n{A}\n{B}") == [B, A]


def test_nothing_in_means_nothing_out():
    for junk in ("", "   ", "wallet\taddress\n", "not an address at all", "0x123"):
        assert config.extract_addresses("robinhood", junk) == []


def test_solana_is_case_sensitive_and_not_lowercased():
    sol = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    got = config.extract_addresses("sol", f'"{sol}"\n')
    assert got == [sol], f"solana addresses must survive verbatim, got {got}"


def test_evm_regex_does_not_match_a_solana_address():
    assert config.extract_addresses("robinhood", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263") == []


def test_is_address_agrees_with_the_extractor():
    for text in SHAPES.values():
        for a in config.extract_addresses("robinhood", text):
            assert config.is_address("robinhood", a)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as exc:
                fails += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
    sys.exit(1 if fails else 0)
