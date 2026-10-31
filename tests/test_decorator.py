"""Tests for :mod:`beancount_utils.decorator`.

Modern pytest style: plain functions, bare ``assert``, fixtures and
parametrization rather than ``unittest.TestCase`` boilerplate.
"""

import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from beancount.core.data import Amount, Posting, Transaction

from beancount_utils.decorator import Decorator, Decoration, normalize_payee


def make_transaction(*, payee="Foo Bar", flag="!", narration="Old", tags=("old",), postings=None):
    if postings is None:
        postings = [Posting("Assets:Cash", 100, None, None, None, None)]
    return Transaction(
        meta=None, date=None, links=None,
        payee=payee, flag=flag, narration=narration, tags=set(tags),
        postings=postings,
    )


@pytest.fixture
def tx():
    return make_transaction()


@pytest.fixture
def decoration():
    # Matches "Foo Bar" and rewrites flag, narration, payee and tags.
    return Decoration({"re": "foo", "flag": "*", "narration": "New", "payee": "Fizz Buzz", "tags": {"new"}})


@pytest.fixture
def decorator(decoration):
    return Decorator([decoration])


# --- Decorator ------------------------------------------------------------


def test_decorate_transaction_applies_decoration(decorator, tx):
    tx2 = decorator.decorate_transaction(tx)
    assert tx2.flag == "*"
    assert tx2.narration == "New"
    assert tx2.payee == "Fizz Buzz"
    assert "new" in tx2.tags
    assert "old" in tx2.tags


def test_decorate_updates_entries_in_place(decorator, tx):
    entries = [tx]
    decorator.decorate(entries)
    assert entries[0].flag == "*"
    assert entries[0].narration == "New"
    assert entries[0].payee == "Fizz Buzz"
    assert "new" in entries[0].tags
    assert "old" in entries[0].tags


def test_decorate_skips_non_transaction(decorator, tx):
    entries = ["not a transaction", tx]
    # Should not raise an error.
    decorator.decorate(entries)
    assert entries[0] == "not a transaction"
    assert entries[1].flag == "*"


def test_decorate_respects_exclude(decoration, tx):
    decorator = Decorator([decoration], exclude=lambda x: True)
    entries = [tx]
    decorator.decorate(entries)
    assert entries[0].flag == "!"  # untouched


def test_first_matching_decoration_takes_precedence(tx):
    dec1 = Decoration({"re": "foo", "flag": "*", "narration": "First"})
    dec2 = Decoration({"re": "foo", "flag": "!", "narration": "Second"})
    decorator = Decorator([dec1, dec2])
    tx2 = decorator.decorate_transaction(tx)
    assert tx2.flag == "*"
    assert tx2.narration == "First"


# --- Decorator.from_dict --------------------------------------------------


@pytest.fixture
def scoped_config():
    return {
        "default": [{"re": "foo", "flag": "*"}],
        "bank": [{"re": "bar", "flag": "!"}],
        "card": [{"re": "baz", "flag": "#"}],
    }


def test_from_dict_with_scope(scoped_config):
    decorator = Decorator.from_dict(scoped_config, scope="bank")
    assert len(decorator.decorations) == 2
    assert any(d.flag == "*" for d in decorator.decorations)  # from default
    assert any(d.flag == "!" for d in decorator.decorations)  # from bank


def test_from_dict_with_default_scope(scoped_config):
    decorator = Decorator.from_dict(scoped_config)
    assert len(decorator.decorations) == 1
    assert decorator.decorations[0].flag == "*"


def test_from_dict_with_missing_scope(scoped_config):
    with pytest.raises(ValueError):
        Decorator.from_dict(scoped_config, scope="nonexistent")


def test_from_dict_invalid_type():
    with pytest.raises(ValueError):
        Decorator.from_dict(["not", "a", "dict"])


# --- Decoration -----------------------------------------------------------


def test_match_true(tx):
    # Case insensitive.
    assert Decoration({"re": "fOo"}).match(tx)


def test_match_false_no_payee():
    tx = make_transaction(payee=None, flag="*", tags=(), postings=[])
    assert not Decoration({"re": "foo"}).match(tx)


def test_match_false_no_match(tx):
    assert not Decoration({"re": "baz"}).match(tx)


def test_requires_a_matcher():
    with pytest.raises(ValueError):
        Decoration({"flag": "*"})


def venmo(number, narration="Payment to Sam", account="Assets:Venmo"):
    posting = Posting(account, Amount(Decimal(number), "USD"), None, None, None, None)
    return make_transaction(payee="Venmo", narration=narration, postings=[posting])


@pytest.mark.parametrize(
    "config, matched",
    [
        pytest.param({"narration_re": "sam"}, True, id="narration"),
        pytest.param({"narration_re": "alex"}, False, id="narration-miss"),
        pytest.param({"account_re": "^Assets:Venmo$"}, True, id="account"),
        pytest.param({"account_re": "Card"}, False, id="account-miss"),
        pytest.param({"min_amount": -1200, "max_amount": -1200}, True, id="exact-amount"),
        pytest.param({"min_amount": "-1300", "max_amount": "-1100.50"}, True, id="amount-range"),
        pytest.param({"max_amount": -1300}, False, id="above-max"),
        pytest.param({"min_amount": 0}, False, id="below-min"),
        pytest.param({"re": "venmo", "narration_re": "sam", "max_amount": 0}, True, id="all-match"),
        pytest.param({"re": "venmo", "narration_re": "alex", "max_amount": 0}, False, id="one-misses"),
    ],
)
def test_match_fields(config, matched):
    assert Decoration(config).match(venmo("-1200.00")) is matched


def test_amount_match_without_units():
    tx = make_transaction(postings=[Posting("Assets:Cash", None, None, None, None, None)])
    assert not Decoration({"min_amount": 0}).match(tx)


def test_decorate_updates_fields(tx):
    d = Decoration({"re": "foo", "flag": "*", "narration": "New", "payee": "Bar", "tags": {"new"}})
    # Transaction is a namedtuple, so _replace returns a new instance.
    tx2 = d.decorate(tx)
    assert tx2.flag == "*"
    assert tx2.narration == "New"
    assert tx2.payee == "Bar"
    assert "new" in tx2.tags
    assert "old" in tx2.tags


@pytest.mark.parametrize("tags", ["new", ["new"]], ids=["string", "list"])
def test_decorate_tags(tags, tx):
    tx2 = Decoration({"re": "foo", "tags": tags}).decorate(tx)
    assert tx2.tags == {"old", "new"}


def test_decorate_adds_posting(tx):
    d = Decoration({"re": "foo", "target_account": "Expenses:Test"})
    tx2 = d.decorate(tx)
    assert tx2.postings[-1].account == "Expenses:Test"
    assert tx2.postings[-1].units == -tx.postings[0].units


# --- Decorator.from_yaml --------------------------------------------------


def test_from_yaml_single_file():
    decoration = {"re": "test1", "flag": "*"}
    with patch("beancount_utils.decorator.load_yaml", return_value=[decoration]) as mock_load_yaml, \
            patch.object(Decorator, "from_list") as mock_from_list:
        Decorator.from_yaml("decorations.yaml")

    mock_load_yaml.assert_called_once_with("decorations.yaml")
    mock_from_list.assert_called_once_with([decoration])


def test_from_yaml_empty_file(tmp_path):
    path = tmp_path / "decorations.yaml"
    path.write_text("")
    assert Decorator.from_yaml(str(path)).decorations == []


def test_from_yaml_empty_sections(tmp_path):
    path = tmp_path / "decorations.yaml"
    path.write_text("default:\nbank:\n  - re: bar\n    flag: '!'\n")
    assert [d.flag for d in Decorator.from_yaml(str(path), scope="bank").decorations] == ["!"]
    assert Decorator.from_yaml(str(path)).decorations == []


def test_from_config_invalid_type():
    with pytest.raises(ValueError):
        Decorator.from_config("not a list")


# --- history ----------------------------------------------------------------


CARD = "Liabilities:Card"


def card_txn(payee, number="-5.00", day=1, category=None, account=CARD):
    postings = [Posting(account, Amount(Decimal(number), "USD"), None, None, None, None)]
    if category:
        postings.append(Posting(category, Amount(-Decimal(number), "USD"), None, None, None, None))
    return Transaction({}, datetime.date(2026, 10, day), "*", payee, "", frozenset(), frozenset(), postings)


@pytest.fixture
def ledger():
    return [
        card_txn("SQ *BLUE BOTTLE 0423", day=1, category="Expenses:Coffee"),
        card_txn("SQ *BLUE BOTTLE 0611", day=9, category="Expenses:Eating-Out"),
        card_txn("SQ *BLUE BOTTLE 0423", day=5, category="Expenses:Coffee"),
        card_txn("Hardware Store", day=3, category="Expenses:Home", account="Liabilities:Other"),
    ]


def test_history_uses_most_recent_entry(ledger):
    entries = [card_txn("SQ *Blue Bottle #9999", day=20)]
    Decorator([], history=True, history_flag="!").decorate(entries, ledger)
    assert [p.account for p in entries[0].postings] == [CARD, "Expenses:Eating-Out"]
    assert entries[0].postings[1].units == Amount(Decimal("5.00"), "USD")
    assert entries[0].flag == "!"


@pytest.mark.parametrize(
    "decorator, entry",
    [
        pytest.param(Decorator([]), card_txn("SQ *BLUE BOTTLE"), id="disabled"),
        pytest.param(Decorator([], history=True), card_txn("Unknown Shop"), id="unknown-payee"),
        pytest.param(Decorator([], history=True), card_txn("Hardware Store"), id="other-account-only"),
        pytest.param(Decorator([], history=True), card_txn("SQ *BLUE BOTTLE", category="Expenses:Gifts"),
                     id="already-categorized"),
    ],
)
def test_history_leaves_entry(decorator, entry, ledger):
    postings = list(entry.postings)
    entries = [entry]
    decorator.decorate(entries, ledger)
    assert entries[0].postings == postings


def test_rules_take_precedence_over_history(ledger):
    entries = [card_txn("SQ *BLUE BOTTLE")]
    rule = Decoration({"re": "blue bottle", "target_account": "Expenses:Rule"})
    Decorator([rule], history=True).decorate(entries, ledger)
    assert [p.account for p in entries[0].postings] == [CARD, "Expenses:Rule"]


@pytest.mark.parametrize(
    "payee, normalized",
    [
        ("SQ *BLUE BOTTLE 0423", "sq blue bottle"),
        ("AMAZON MKTPL*AB12CD", "amazon mktpl"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_payee(payee, normalized):
    assert normalize_payee(payee) == normalized


# --- Decorator.append_* ---------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"re": "foo", "flag": "*"}, id="dict"),
        pytest.param(Decoration({"re": "foo", "flag": "*"}), id="decoration"),
    ],
)
def test_append_list_decorates(extra, tx):
    decorator = Decorator([])
    decorator.append_list([extra])
    assert decorator.decorate_transaction(tx).flag == "*"


def test_append_yaml_with_scope(scoped_config):
    decorator = Decorator([])
    with patch("beancount_utils.decorator.load_yaml", return_value=scoped_config):
        decorator.append_yaml("decorations.yaml", scope="card")
    assert [d.flag for d in decorator.decorations] == ["#", "*"]
    assert decorator.decorate_transaction(make_transaction(payee="baz")).flag == "#"
