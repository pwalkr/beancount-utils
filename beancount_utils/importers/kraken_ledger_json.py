import datetime
import json

from decimal import Decimal

import beangulp

from beancount.core.data import Amount, Balance, Posting, Transaction, new_metadata
from beancount.core.position import CostSpec
from beanprice import price as beanprice

from beancount_utils.deduplicate import mark_duplicate_entries, extract_out_of_place


assets_remap = {
    "XETH": "ETH",
    "XLTC": "LTC",
    "XXBT": "BTC",
    "XXDG": "DOGE",
    "XXLM": "XLM",
    "XXRP": "XRP",
    "ZUSD": "USD",
}


class Importer(beangulp.Importer):
    """An importer for Kraken Ledger JSON Export."""

    def __init__(self, base_account, base_currency='USD', fee_account=None, pnl_account="Income:PnL", stake_account="Income:Staking", price_cache=None):
        self.base_account = base_account
        self.base_currency = base_currency
        self.pnl_account = pnl_account
        self.fee_account = fee_account
        self.stake_account = stake_account
        self.price_cache = price_cache

    def identify(self, filepath):
        if not filepath.lower().endswith(".json"):
            return False

        # Match the account id.
        with open(filepath) as f:
            data = json.load(f)
        return data and "result" in data and "ledger" in data['result']

    def account(self, filepath):
        return self.base_account

    def extract(self, filepath, existing):
        entries = []

        # Initialize beanprice cache and map of commodity:sources
        currencies = beanprice.find_currencies_declared(existing)
        commodity_sources = { currency[0]:currency[2] for currency in currencies }
        if self.price_cache:
            beanprice.setup_cache(self.price_cache, False)

        with open(filepath) as f:
            data = json.load(f)

        for time, gmap in group_ledgers(data['result']['ledger']).items():
            for ledger_type, group in gmap.items():
                meta = new_metadata(filepath, 0)
                date = datetime.datetime.fromtimestamp(time)
                if ledger_type == 'staking':
                    entries.append(self._extract_staking(date.date(), meta, group, commodity_sources))
                elif ledger_type == 'trade':
                    narration = extract_narration(group, self.base_currency)
                    postings = self._extract_postings(group)
                    entries.append(Transaction(meta, date.date(), '*', None, narration, frozenset(), frozenset(), postings))
                else:
                    print(f"Unknown ledger type: {ledger_type}\n{group}")
                if False:
                    entries.extend(extract_balances(group, filepath, date, self.base_account))

        return entries

    def get_asset_account(self, asset):
        return self.base_account + ':' + asset

    def _extract_staking(self, date, meta, group, commodity_sources):
        if len(group) != 1:
            raise ValueError("Staking group should contain exactly one entry.")
        ledger = group[0]
        narration = "Staked " + ledger['asset']
        account = self.get_asset_account(ledger['asset'])
        total = Amount(Decimal(ledger['amount']), ledger['asset'])
        fee = Amount(Decimal(ledger['fee']), ledger['asset'])
        amount = Amount(total.number - fee.number, ledger['asset'])

        # Get cost, or set posting meta to highlight missing source
        cost = None
        price = None
        pmeta = None
        if ledger['asset'] in commodity_sources:
            srcs = commodity_sources[ledger['asset']]
            dated_price = beanprice.DatedPrice(self.base_currency, None, date, srcs)
            price = beanprice.fetch_price(dated_price)
            if price:
                cost = CostSpec(price.amount.number, None, price.amount.currency, None, None, None)
        else:
            pmeta = {'notice': f"{ledger['asset']} not found in commodities"}

        postings = [
            Posting(self.stake_account, None, None, None, None, None),
            Posting(account, amount, cost, None, None, pmeta)
        ]
        if self.fee_account:
            postings.append(Posting(self.fee_account, fee, None, price, None, None))
        return Transaction(meta, date, '*', None, narration, frozenset(), frozenset(), postings)

    def _extract_postings(self, group):
        postings = []
        if is_sale(group, self.base_currency):
            postings.append(Posting(self.pnl_account, None, None, None, None, None))
        for ledger in group:
            asset_account = self.base_account + ':' + ledger['asset']
            amount = Decimal(ledger['amount'])
            # Ignore 0-amount KFEE entries
            if ledger['asset'] == 'KFEE' and amount < 0.01:
                continue
            cost = extract_cost(ledger, group, self.base_currency)
            amount = Amount(amount, ledger['asset'])
            postings.append(Posting(asset_account, amount, cost, None, None, None))
        return postings

    def deduplicate(self, entries, existing):
        mark_duplicate_entries(entries, existing, self.base_account)
        entries.extend(extract_out_of_place(existing, entries, self.base_account))

def sanitize_assets(ledger):
    for tid, entry in ledger.items():
        # Trim .F, .S, etc.
        entry['asset'] = entry['asset'].split('.')[0]
        if entry['asset'] in assets_remap:
            entry['asset'] = assets_remap[entry['asset']]
        yield tid, entry

def group_ledgers(ledger):
    groups = {}
    for tid, entry in sanitize_assets(ledger):
        time = entry['time']
        if time not in groups:
            groups[time] = {}
        if entry['type'] not in groups[time]:
            groups[time][entry['type']] = []
        groups[time][entry['type']].append(entry)
    return groups

def extract_narration(group, base_currency):
    narration = None
    for entry in group:
        if entry['asset'] != base_currency:
            if Decimal(entry['amount']) > 0:
                narration = "Buy " + entry['asset']
            else:
                narration = "Sell " + entry['asset']
    return narration

def extract_balances(group, filepath, date, base_account):
    # Dates are considered the start of a day, must succeed transaction date
    date = date + datetime.timedelta(days=1)

    for ledger in group:
        meta = new_metadata(filepath, 0)
        account = base_account + ':' + ledger['asset']
        amount = Amount(Decimal(ledger['balance']), ledger['asset'])
        yield Balance(meta, date.date(), account, amount, None, None)

def extract_cost(ledger, group, base_currency):
    if ledger['asset'] == base_currency:
        return None
    cost = None
    for entry in group:
        if entry['asset'] == base_currency:
            amount = Decimal(entry['amount'])
            if amount < 0:
                # Reduction in currenct = buy
                #cost = CostSpec(Decimal(0), -Decimal(entry['amount']), base_currency, None, None, None)
                cost = CostSpec(None, -Decimal(entry['amount']), base_currency, None, None, None)
            else:
                cost = CostSpec(None, None, None, None, None, None)
    return cost

def is_sale(group, base_currency):
    for ledger in group:
        if ledger['asset'] != base_currency:
            if Decimal(ledger['amount']) < 0:
                return True
    return False
