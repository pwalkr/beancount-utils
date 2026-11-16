from decimal import Decimal

import pytest

from beancount.core.data import Amount

from beancount_utils.importers.kraken_ledger_json import extract_price


def _ledger(asset, amount):
    return {'asset': asset, 'amount': amount}


def test_sale_has_per_unit_price():
    btc = _ledger('BTC', '-0.0493757900')
    group = [_ledger('USD', '3913.0314'), btc]
    price = extract_price(btc, group, 'USD')
    assert price.currency == 'USD'
    assert price.number == Decimal('79250.001')


def test_exact_price_not_in_exponent_notation():
    btc = _ledger('BTC', '-0.05')
    price = extract_price(btc, [_ledger('USD', '4000'), btc], 'USD')
    assert str(price.number) == '80000'


@pytest.mark.parametrize('ledger, group', [
    # Buy: no price annotation, cost carries the total
    (_ledger('BTC', '0.05'), [_ledger('USD', '-4000'), _ledger('BTC', '0.05')]),
    # Base currency posting never gets a price
    (_ledger('USD', '4000'), [_ledger('USD', '4000'), _ledger('BTC', '-0.05')]),
])
def test_no_price(ledger, group):
    assert extract_price(ledger, group, 'USD') is None
