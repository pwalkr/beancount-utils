"""Tests for posting matching in :mod:`beancount_utils.deduplicate`."""

import datetime
from decimal import Decimal

import pytest

from beancount.core.data import Amount, Posting, Transaction
from beangulp.extract import DUPLICATE

from beancount_utils.deduplicate import extract_out_of_place, mark_duplicate_entries, match_postings, wrap_postings

ACCOUNT = "Liabilities:Card"
ID_KEY = "simplefin_id"
BASE_DATE = datetime.date(2026, 10, 5)


def make_txn(number, *, id=None, days=0, account=ACCOUNT):
    """Build a one-posting ``Transaction`` with fresh, mutable meta."""
    meta = {ID_KEY: id} if id is not None else {}
    posting = Posting(account, Amount(Decimal(number), "USD"), None, None, None, meta)
    date = BASE_DATE + datetime.timedelta(days=days)
    return Transaction({"filename": "test", "lineno": 0}, date, "*", "Payee", "", frozenset(), frozenset(), [posting])


def duplicate_of(entry):
    return entry.meta.get(DUPLICATE)


# --- mark_duplicate_entries ----------------------------------------------


@pytest.mark.parametrize(
    "incoming, existing, matched",
    [
        pytest.param(make_txn("-5", id="a"), make_txn("-5", id="a"), True, id="same-id"),
        pytest.param(make_txn("-5", id="a"), make_txn("-5", id="b"), False, id="different-id-same-amount"),
        pytest.param(make_txn("-5", id="a"), make_txn("-7", id="a", days=10), True, id="same-id-ignores-amount-and-window"),
        pytest.param(make_txn("-5", id="a"), make_txn("-5"), True, id="existing-without-id-falls-back"),
        pytest.param(make_txn("-5", id="a"), make_txn("-7"), False, id="fallback-still-checks-amount"),
    ],
)
def test_id_matching(incoming, existing, matched):
    mark_duplicate_entries([incoming], [existing], ACCOUNT, id_key=ID_KEY)
    assert (duplicate_of(incoming) is existing) is matched


def test_ids_ignored_without_id_key():
    incoming, existing = make_txn("-5", id="a"), make_txn("-5", id="b")
    mark_duplicate_entries([incoming], [existing], ACCOUNT)
    assert duplicate_of(incoming) is existing


def test_exact_id_match_wins_over_earlier_fuzzy_candidate():
    """A same-amount entry without an id must not steal the real match."""
    incoming = make_txn("-5", id="a")
    no_id, with_id = make_txn("-5"), make_txn("-5", id="a", days=1)
    mark_duplicate_entries([incoming], [no_id, with_id], ACCOUNT, id_key=ID_KEY)
    assert duplicate_of(incoming) is with_id


def test_repeated_amounts_pair_by_id():
    incoming = [make_txn("-5", id="a"), make_txn("-5", id="b")]
    existing = [make_txn("-5", id="b"), make_txn("-5", id="a")]
    mark_duplicate_entries(incoming, existing, ACCOUNT, id_key=ID_KEY)
    assert [duplicate_of(e) for e in incoming] == [existing[1], existing[0]]


# --- extract_out_of_place -------------------------------------------------


def test_out_of_place_flags_unknown_id_with_same_amount():
    """An existing entry with a stale id is out of place even if amounts agree."""
    incoming = [make_txn("-5", id="a")]
    existing = [make_txn("-5", id="stale")]
    out = extract_out_of_place(existing, incoming, ACCOUNT, id_key=ID_KEY)
    assert len(out) == 1
    assert "OUT_OF_PLACE" in out[0].tags
    assert out[0].postings[0].flag == "!"


def test_out_of_place_id_match_wins_over_earlier_id_less_posting():
    """An id-less ledger entry listed first must not take the id'd entry's match."""
    no_id, with_id = make_txn("-5"), make_txn("-5", id="a")
    out = extract_out_of_place([no_id, with_id], [make_txn("-5", id="a")], ACCOUNT, id_key=ID_KEY)
    assert len(out) == 1
    assert ID_KEY not in out[0].postings[0].meta


def test_each_posting_matched_once():
    incoming = [make_txn("-5", id="a"), make_txn("-5")]
    existing = [make_txn("-5", id="a"), make_txn("-5")]
    pairs = list(match_postings(wrap_postings(incoming, ACCOUNT, ID_KEY),
                                wrap_postings(existing, ACCOUNT, ID_KEY),
                                datetime.timedelta(days=2)))
    assert len(pairs) == 2
    assert len({id(p) for p, _ in pairs}) == 2
    assert len({id(c) for _, c in pairs}) == 2


def test_out_of_place_accepts_matching_id():
    incoming = [make_txn("-5", id="a")]
    existing = [make_txn("-5", id="a")]
    assert extract_out_of_place(existing, incoming, ACCOUNT, id_key=ID_KEY) == []
