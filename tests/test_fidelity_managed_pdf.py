"""Tests for the Fidelity multi-account INVESTMENT REPORT importer."""
from datetime import date
from decimal import Decimal

from beancount.core.data import Amount, Price

from beancount_utils.importers import fidelity_managed_pdf as mod


# Brokerage layout: the ETP table has no percent-of-holdings column, a
# position opened during the period shows "unavailable" instead of a
# beginning market value, and the description wraps over three lines.
BROKERAGE_STATEMENT = """\
INVESTMENT REPORT
April 1, 2026 - April 30, 2026
Account # 266-014480
Holdings
Core Account
FIDELITY GOVERNMENT MONEY $35.43 21.780 $1.0000 $21.78 not applicable not applicable $0.81
MARKET (SPAXX) 3.720%
Exchange Traded Products
Other ETPs
PROSHARES TRUST COINDESK 20 unavailable 28.000 $21.7950 $610.26 $600.10 $10.16 -
CRYPTO -
ETF (KRYP)
unavailable 610.26 600.10 10.16 -
Total Other ETPs (4% of account holdings)
Activity
Dividends, Interest & Other Income
04/08 PROSHARES TRUST COINDESK 20 74350P683 Dividend Received - - 0.06
CRYPTO
ETF
Total Dividends, Interest & Other Income $0.06
"""

# Managed layout: a percent-of-holdings column precedes the beginning
# market value and only the first row of a table prints it with a "%".
MANAGED_STATEMENT = """\
INVESTMENT REPORT
April 1, 2026 - April 30, 2026
Account # Y80-409811
Holdings
Stock Funds
FIDELITY FLEX 500 INDEX 38.74% $16,805.58 605.993 $30.6000 $18,543.39 $12,267.38 $6,276.01 $201.80
FUND (FDFIX) 1.090%
FIDELITY FLEX 18.20 8,073.10 471.835 18.4700 8,714.79 6,031.85 2,682.94 227.90
INTERNATIONAL INDEX 2.620
(FITFX)
Total Stock Funds 61.24% $26,747.64 $29,321.05 $19,694.53 $9,626.52 $429.70
Activity
FIDELITY FLEX 500 INDEX FUND 04/10 315911685 Dividend Received - - $48.95
04/10 315911685 Reinvestment 1.693 28.91000 -48.95
"""


def extract(monkeypatch, statement, account_id, account):
    monkeypatch.setattr(mod, "pdf_to_pages", lambda path: [statement])
    importer = mod.Importer(
        accounts=[{"account_id": account_id, "account": account}]
    )
    return importer.extract("statement.pdf", [])


def test_holding_without_beginning_value_is_parsed():
    holdings = mod.parse_holdings(BROKERAGE_STATEMENT.splitlines())
    assert holdings["KRYP"]["quantity"] == Decimal("28.000")
    assert holdings["KRYP"]["price"] == Decimal("21.7950")
    assert holdings["KRYP"]["ending_value"] == Decimal("610.26")
    assert not holdings["KRYP"]["is_cash"]


def test_digits_in_a_security_name_are_kept():
    holdings = mod.parse_holdings(BROKERAGE_STATEMENT.splitlines())
    assert holdings["KRYP"]["name"] == "PROSHARES TRUST COINDESK 20 CRYPTO ETF"


def test_percent_of_holdings_column_does_not_shift_the_others():
    holdings = mod.parse_holdings(MANAGED_STATEMENT.splitlines())
    # "18.20" is the percent of holdings, not the quantity.
    assert holdings["FITFX"]["name"] == "FIDELITY FLEX INTERNATIONAL INDEX"
    assert holdings["FITFX"]["quantity"] == Decimal("471.835")
    assert holdings["FITFX"]["price"] == Decimal("18.4700")
    assert holdings["FITFX"]["ending_value"] == Decimal("8714.79")


def test_dividend_for_wrapped_name_resolves_its_ticker(monkeypatch):
    entries = extract(
        monkeypatch, BROKERAGE_STATEMENT,
        "266-014480", "Assets:US:Fidelity:RothIRA",
    )
    dividends = [e for e in entries if getattr(e, "narration", None)]
    assert [e.narration for e in dividends] == ["Dividend - KRYP"]
    assert dividends[0].postings[1].units.number == Decimal("0.06")


def test_reinvestment_debits_cash(monkeypatch):
    entries = extract(
        monkeypatch, MANAGED_STATEMENT, "Y80-409811", "Assets:US:Fidelity:HSA"
    )
    reinvest = next(
        e for e in entries if getattr(e, "narration", "") == "Reinvest FDFIX"
    )
    cash, shares = reinvest.postings
    assert cash.account == "Assets:US:Fidelity:HSA:USD"
    assert cash.units.number == Decimal("-48.95")
    assert shares.units.number == Decimal("1.693")


def test_price_directive_per_held_commodity(monkeypatch):
    entries = extract(
        monkeypatch, MANAGED_STATEMENT, "Y80-409811", "Assets:US:Fidelity:HSA"
    )
    prices = {e.currency: e for e in entries if isinstance(e, Price)}
    assert set(prices) == {"FDFIX", "FITFX"}
    assert prices["FITFX"].date == date(2026, 4, 30)
    assert prices["FITFX"].amount == Amount(Decimal("18.4700"), "USD")


def test_cash_fund_is_not_priced(monkeypatch):
    entries = extract(
        monkeypatch, BROKERAGE_STATEMENT,
        "266-014480", "Assets:US:Fidelity:RothIRA",
    )
    prices = [e for e in entries if isinstance(e, Price)]
    assert [e.currency for e in prices] == ["KRYP"]
