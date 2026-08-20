"""Tests for the Merrill Edge PDF importer's period-end price directives."""

import datetime
from decimal import Decimal

import pytest

from beancount.core.data import Price

from beancount_utils.importers import merrill_pdf


STATEMENT = """\
CMA® ACCOUNT
July 01, 2026 - July 31, 2026
CASH/MONEY ACCOUNTS Total Estimated Estimated Estimated Est. Annual
CASH 0.58 0.58 .58
ML DIRECT DEPOSIT PROGRAM 688.00 688.00 1.0000 688.00 .01
TOTAL 688.58 688.58 .01
GOVERNMENT AND AGENCY SECURITIES Adjusted/Total Estimated Estimated Unrealized
U.S. TREASURY STRIP 11/27/24 19,000 8,850.97 41.6500 7,913.50 (937.47)
ZERO% NOV 15 2042 MOODY'S: *** S&P: *** CUSIP: 912834LX4
TOTAL 19,000 8,850.97 7,913.50 (937.47)
MUTUAL FUNDS/CLOSED END FUNDS/UITs/ETPs Total Estimated Estimated Unrealized
ISHARES 0-3 908.0000 91,247.97 100.7100 91,444.68 196.71 91,247 196 3,469
MONTH TREASURY BOND ETF CURRENT YIELD 3.793% SYMBOL: SGOV Initial Purchase: 05/02/25
V A N G .UARD 500 INDEX FUND 2.0000 948.22 686.6500 1,373.30 425.08 948 425 15
SHS ETF CURRENT YIELD 1.069% SYMBOL: VOO Initial Purchase: 08/05/24
Subtotal (Fixed Income) 91,444.68
"""


@pytest.fixture
def entries(monkeypatch, tmp_path):
    monkeypatch.setattr(merrill_pdf, "pdf_to_pages", lambda _: [STATEMENT])
    importer = merrill_pdf.Importer("Assets:Merrill")
    return importer.extract(str(tmp_path / "statement.pdf"), [])


@pytest.fixture
def prices(entries):
    return {e.currency: e for e in entries if isinstance(e, Price)}


def test_prices_cover_every_holding(prices):
    assert set(prices) == {"SGOV", "VOO", "B912834LX4"}


@pytest.mark.parametrize(
    "commodity, expected",
    [
        ("SGOV", Decimal("100.7100")),
        ("VOO", Decimal("686.6500")),
        # Bonds are quoted per $100 of face value; quantity counts face dollars.
        ("B912834LX4", Decimal("0.4165")),
    ],
)
def test_price_is_per_unit(prices, commodity, expected):
    assert prices[commodity].amount.number == expected
    assert prices[commodity].amount.currency == "USD"


def test_prices_are_dated_period_end(prices):
    assert all(p.date == datetime.date(2026, 7, 31) for p in prices.values())


def test_price_times_quantity_matches_statement_market_value(entries, prices):
    holdings, _ = merrill_pdf.parse_holdings(STATEMENT.splitlines())
    for commodity, holding in holdings.items():
        value = holding["quantity"] * prices[commodity].amount.number
        assert value == holding["ending_value"]
