"""Tests for :mod:`beancount_utils.importers.kraken_balance_json`."""

import json
from decimal import Decimal

import pytest

from beancount_utils.importers.kraken_balance_json import Importer

ACCOUNT = "Assets:Kraken"


@pytest.fixture
def export(tmp_path):
    """A balance export with a real balance, dust, an exact zero and a staked split."""
    path = tmp_path / "balance.json"
    path.write_text(json.dumps({"error": [], "result": {
        "XXBT": "0.0123400000",
        "XETH": "0.0000000004",
        "XXDG": "0.0000000000",
        "SOL03": "1.5000000000",
        "SOL03.S": "-1.5000000000",
        "ZUSD": "0.0000010000",
    }}))
    return str(path)


def balances(entries):
    return {e.amount.currency: e.amount.number for e in entries}


def test_drops_zero_balances_by_default(export):
    entries = Importer(ACCOUNT).extract(export, [])
    # USD sits exactly at the tolerance, which is not below it
    assert balances(entries) == {"BTC": Decimal("0.0123400000"), "USD": Decimal("0.0000010000")}
    assert [e.account for e in entries] == [f"{ACCOUNT}:BTC", f"{ACCOUNT}:USD"]


def test_keeps_zero_balances_when_disabled(export):
    entries = Importer(ACCOUNT, drop_zero=False).extract(export, [])
    assert set(balances(entries)) == {"BTC", "ETH", "DOGE", "SOL", "USD"}


def test_drop_zero_custom_tolerance(export):
    entries = Importer(ACCOUNT, zero_tolerance=Decimal("0.001")).extract(export, [])
    assert set(balances(entries)) == {"BTC"}
