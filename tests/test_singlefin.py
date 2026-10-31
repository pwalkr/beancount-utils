"""Tests for :mod:`beancount_utils.importers.singlefin`."""

import datetime
import json
from decimal import Decimal

import pytest

from beancount.core.data import Amount, Posting, Transaction
from beangulp.extract import DUPLICATE

from beancount_utils.importers.singlefin import ID_KEY, Importer

ACCOUNT = "Liabilities:Card"
ACCTID = "ACT-123"


def sfin_txn(id, amount, day):
    timestamp = int(datetime.datetime(2026, 10, day, 12).timestamp())
    return {"id": id, "posted": timestamp, "amount": amount,
            "description": "COFFEE   SHOP", "payee": "Coffee Shop"}


@pytest.fixture
def importer():
    return Importer(ACCOUNT, ACCTID)


@pytest.fixture
def export(tmp_path):
    """Write a SimpleFIN export with two same-amount transactions."""
    path = tmp_path / "simplefin.json"
    path.write_text(json.dumps({"accounts": [{
        "id": ACCTID,
        "transactions": [sfin_txn("TRN-1", "-5.00", 4), sfin_txn("TRN-2", "-5.00", 5)],
    }]}))
    return str(path)


def ledger_txn(number, day, id=None):
    meta = {ID_KEY: id} if id is not None else {}
    posting = Posting(ACCOUNT, Amount(Decimal(number), "USD"), None, None, None, meta)
    return Transaction({"filename": "ledger", "lineno": day}, datetime.date(2026, 10, day),
                       "*", "Coffee Shop", "", frozenset(), frozenset(), [posting])


def test_extract_stores_transaction_id(importer, export):
    entries = importer.extract(export, [])
    assert [e.postings[0].meta[ID_KEY] for e in entries] == ["TRN-1", "TRN-2"]


def test_deduplicate_matches_by_id(importer, export):
    """Only the ledger's TRN-2 is known; TRN-1 is new despite the same amount."""
    entries = importer.extract(export, [])
    existing = [ledger_txn("-5.00", 4, id="TRN-2")]
    importer.deduplicate(entries, existing)
    assert DUPLICATE not in entries[0].meta
    assert entries[1].meta[DUPLICATE] is existing[0]


def test_deduplicate_falls_back_for_ledger_without_ids(importer, export):
    entries = importer.extract(export, [])
    existing = [ledger_txn("-5.00", 4)]
    importer.deduplicate(entries, existing)
    assert entries[0].meta[DUPLICATE] is existing[0]
    assert DUPLICATE not in entries[1].meta
