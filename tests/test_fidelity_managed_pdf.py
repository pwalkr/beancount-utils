"""Tests for the Fidelity multi-account INVESTMENT REPORT importer."""
from datetime import date
from decimal import Decimal

from beancount.core.data import Amount, Price, Transaction

from beancount_utils.importers.fidelity_pdf import resolve_ticker

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


# The Holdings and Activity tables abbreviate names differently, and not
# in the same direction: "PROSHARES TR COIN 20 CRYP ETF" (Holdings),
# "PROSHARES TRUST COINDESK 20 CRYPTO ETF" (Dividends) and "PROSHARES
# TRUST COINDESK 20" (Securities Bought & Sold).
ABBREVIATED_STATEMENT = """\
INVESTMENT REPORT
May 1, 2026 - May 31, 2026
Account # 123-654987
Holdings
Stocks
Common Stock
ADVANCED MICRO DEVICES INC (AMD) 2,481.43 5.000 516.1000 2,580.50 918.00 1,662.50 -
-
PROSHARES TR COIN 20 CRYP ETF 610.26 67.000 20.8195 1,394.90 1,483.06 -88.16 -
(KRYP) -
Total Common Stock (100% of account $15,417.03 $17,410.30 $12,722.95 $4,687.35 $35.75
Activity
Securities Bought & Sold
05/11 ADVANCED MICRO DEVICES INC 007903107 You Sold -2.000 $440.75000 $330.73 -$0.02 $881.48
Transaction Profit: $550.75
05/11 PROSHARES TRUST COINDESK 20 74350P683 You Bought 39.000 22.64000 - -882.96
CRYPTO
ETF
Total Securities Bought - - -$882.96
Dividends, Interest & Other Income
05/07 PROSHARES TRUST COINDESK 20 74350P683 Dividend Received - - $0.15
CRYPTO
ETF
Total Dividends, Interest & Other Income $0.15
"""

# Managed layout with a Cost Basis column: only the first row of each
# security block names it, the rest carry just a date and a CUSIP.
SECURITY_BLOCK_STATEMENT = """\
INVESTMENT REPORT
May 1, 2026 - May 31, 2026
Account # 123-45689
Holdings
Stock Funds
FIDELITY FLEX 500 INDEX 37.89% $18,543.39 579.154 $32.2600 $18,683.51 $11,479.65 $7,203.86 $192.86
FUND (FDFIX) 1.030%
Bond Funds
FIDELITY FLEX CONS 4.48% $2,088.92 220.506 $10.0300 $2,211.68 $2,205.08 $6.60 $96.15
INCOME BOND FUND (FJTDX) 4.350%
Activity
FIDELITY FLEX 500 INDEX FUND 05/28 315911685 You Sold -26.839 $31.97000 $787.73 $858.04
Short-term gain: $70.31
refer to confirm for Lot detail
FIDELITY FLEX CONS INCOME BOND 05/28 31635T500 You Bought 11.515 $10.03000 - -$115.50
FUND
05/29 31635T500 Dividend Received - - - 7.26
05/29 31635T500 Reinvestment 0.724 10.03000 - -7.26
"""


def narrations(entries):
    return [e.narration for e in entries if isinstance(e, Transaction)]


def test_abbreviated_holdings_name_resolves_dividend_ticker(monkeypatch):
    entries = extract(
        monkeypatch, ABBREVIATED_STATEMENT,
        "123-654987", "Assets:US:Fidelity:RothIRA",
    )
    assert "Dividend - KRYP" in narrations(entries)


def test_abbreviated_holdings_name_resolves_trade_ticker(monkeypatch):
    entries = extract(
        monkeypatch, ABBREVIATED_STATEMENT,
        "123-654987", "Assets:US:Fidelity:RothIRA",
    )
    assert "Buy KRYP" in narrations(entries)


def test_transaction_cost_column_is_ignored(monkeypatch):
    entries = extract(
        monkeypatch, ABBREVIATED_STATEMENT,
        "123-654987", "Assets:US:Fidelity:RothIRA",
    )
    sell = next(e for e in entries if getattr(e, "narration", "") == "Sell AMD")
    assert sell.postings[0].units.number == Decimal("881.48")
    assert sell.postings[1].units.number == Decimal("-2.000")


def test_rows_without_a_name_use_their_security_block(monkeypatch):
    entries = extract(
        monkeypatch, SECURITY_BLOCK_STATEMENT,
        "123-45689", "Assets:US:Fidelity:HSA",
    )
    assert set(narrations(entries)) == {
        "Sell FDFIX", "Buy FJTDX", "Dividend - FJTDX", "Reinvest FJTDX",
    }
    reinvest = next(
        e for e in entries if getattr(e, "narration", "") == "Reinvest FJTDX"
    )
    assert reinvest.postings[0].units.number == Decimal("-7.26")


def test_abbreviation_must_be_unambiguous():
    names = {
        "PROSHARES TRUST COINDESK 20 CRYPTO ETF": "KRYP",
        "PROSHARES TRUST COINDESK 20 FUTURES ETF": "KRYPF",
    }
    # Abbreviated down to the tokens the two holdings share.
    assert resolve_ticker("PROSHARES TR COIN 20", names) is None
    assert resolve_ticker("PROSHARES TR COIN 20 CRYP ETF", names) == "KRYP"
