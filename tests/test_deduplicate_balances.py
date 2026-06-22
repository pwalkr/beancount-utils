"""Tests for ``mark_duplicate_balances`` in :mod:`beancount_utils.deduplicate`.

Written in modern pytest style: plain functions, bare ``assert``, fixtures and
parametrization rather than ``unittest.TestCase`` boilerplate.
"""

import datetime
from decimal import Decimal

import pytest

from beancount.core.data import Amount, Balance
from beangulp.extract import DUPLICATE

from beancount_utils.deduplicate import mark_duplicate_balances

ACCOUNT = "Assets:Kraken:BTC"
BASE_DATE = datetime.date(2026, 6, 20)


def make_balance(number, *, date=BASE_DATE, account=ACCOUNT, currency="BTC"):
    """Build a ``Balance`` directive with a fresh, mutable ``meta`` dict."""
    amount = Amount(Decimal(str(number)), currency)
    return Balance({}, date, account, amount, None, None)


@pytest.fixture
def existing():
    """A single existing balance the incoming entries are compared against."""
    return [make_balance("1.50000000")]


def is_marked(entry):
    return DUPLICATE in entry.meta


# --- matching cases -------------------------------------------------------


@pytest.mark.parametrize(
    "incoming",
    [
        pytest.param(make_balance("1.50000000"), id="exact-match"),
        pytest.param(make_balance("1.50005000"), id="within-tolerance-above"),
        pytest.param(make_balance("1.49995000"), id="within-tolerance-below"),
        pytest.param(make_balance("1.50010000"), id="tolerance-boundary-above"),
        pytest.param(make_balance("1.49990000"), id="tolerance-boundary-below"),
        pytest.param(make_balance("1.5", date=BASE_DATE + datetime.timedelta(days=2)), id="window-edge-after"),
        pytest.param(make_balance("1.5", date=BASE_DATE - datetime.timedelta(days=2)), id="window-edge-before"),
    ],
)
def test_marks_balances_within_window_and_tolerance(existing, incoming):
    entries = [incoming]
    mark_duplicate_balances(entries, existing)
    assert is_marked(incoming)
    # The candidate (not the incoming entry) is recorded as the duplicate target.
    assert incoming.meta[DUPLICATE] is existing[0]


# --- non-matching cases ---------------------------------------------------


@pytest.mark.parametrize(
    "incoming",
    [
        pytest.param(make_balance("1.60000000"), id="amount-outside-tolerance"),
        pytest.param(make_balance("1.50011000"), id="just-outside-tolerance"),
        pytest.param(make_balance("1.5", date=BASE_DATE + datetime.timedelta(days=3)), id="outside-window-after"),
        pytest.param(make_balance("1.5", date=BASE_DATE - datetime.timedelta(days=3)), id="outside-window-before"),
        pytest.param(make_balance("1.5", account="Assets:Kraken:ETH"), id="different-account"),
        pytest.param(make_balance("1.5", currency="ETH"), id="different-currency"),
    ],
)
def test_does_not_mark_non_matching_balances(existing, incoming):
    entries = [incoming]
    mark_duplicate_balances(entries, existing)
    assert not is_marked(incoming)


# --- behavioral details ---------------------------------------------------


def test_ignores_non_balance_directives(existing):
    # A bare string and a None stand in for unrelated directive types; the
    # function should skip them without raising.
    entries = ["not a directive", None, make_balance("1.5")]
    mark_duplicate_balances(entries, existing)
    assert is_marked(entries[2])


def test_records_first_matching_candidate():
    earlier = make_balance("1.50000000", date=BASE_DATE - datetime.timedelta(days=1))
    later = make_balance("1.50000000", date=BASE_DATE + datetime.timedelta(days=1))
    incoming = make_balance("1.50000000")
    mark_duplicate_balances([incoming], [earlier, later])
    assert incoming.meta[DUPLICATE] is earlier


def test_respects_custom_tolerance(existing):
    incoming = make_balance("1.51000000")  # 0.01 off, outside default tolerance
    mark_duplicate_balances([incoming], existing, tolerance=Decimal("0.1"))
    assert is_marked(incoming)


def test_respects_custom_window(existing):
    incoming = make_balance("1.5", date=BASE_DATE + datetime.timedelta(days=10))
    mark_duplicate_balances([incoming], existing, window=datetime.timedelta(days=14))
    assert is_marked(incoming)


@pytest.mark.parametrize(
    "entries, context",
    [
        pytest.param([], [make_balance("1.5")], id="no-entries"),
        pytest.param([make_balance("1.5")], [], id="no-context"),
    ],
)
def test_empty_inputs_are_noops(entries, context):
    # Capture identities before the call so we can assert nothing was marked.
    mark_duplicate_balances(entries, context)
    assert all(not is_marked(e) for e in entries)


def test_does_not_mutate_unmatched_entry_meta(existing):
    incoming = make_balance("9.99999999")  # nowhere near existing
    mark_duplicate_balances([incoming], existing)
    assert incoming.meta == {}
