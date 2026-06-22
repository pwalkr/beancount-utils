"""Tests for :mod:`beancount_utils.decorator`.

Modern pytest style: plain functions, bare ``assert``, fixtures and
parametrization rather than ``unittest.TestCase`` boilerplate.
"""

from unittest.mock import patch

import pytest

from beancount.core.data import Posting, Transaction

from beancount_utils.decorator import Decorator, Decoration


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


def test_decorate_updates_fields(tx):
    d = Decoration({"re": "foo", "flag": "*", "narration": "New", "payee": "Bar", "tags": {"new"}})
    # Transaction is a namedtuple, so _replace returns a new instance.
    tx2 = d.decorate(tx)
    assert tx2.flag == "*"
    assert tx2.narration == "New"
    assert tx2.payee == "Bar"
    assert "new" in tx2.tags
    assert "old" in tx2.tags


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
