import datetime
import json

from decimal import Decimal

import beangulp

from beancount.core.data import Amount, Balance, Posting, Transaction, new_metadata
from beancount.core.position import CostSpec

from beancount_utils.deduplicate import mark_duplicate_entries, extract_out_of_place


assets = {
    "XETH": "ETH",
    "XLTC": "LTC",
    "XXBT": "BTC",
    "XXDG": "DOGE",
    "ZUSD": "USD",
}


class Importer(beangulp.Importer):
    """An importer for Kraken Ledger JSON Export."""

    def __init__(self, base_account, base_currency='USD', pnl_account="Income:PnL"):
        self.base_account = base_account
        self.base_currency = base_currency
        self.pnl_account = pnl_account

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

        with open(filepath) as f:
            data = json.load(f)

        for time, group in group_ledgers(data['result']['ledger']).items():
            meta = new_metadata(filepath, 0)
            date = datetime.datetime.fromtimestamp(time)
            narration = extract_narration(group, self.base_currency)
            postings = extract_postings(group, self.base_account, self.base_currency, self.pnl_account)
            entries.append(Transaction(meta, date.date(), '*', None, narration, frozenset(), frozenset(), postings))
            entries.extend(extract_balances(group, filepath, date, self.base_account))

        return entries

    def deduplicate(self, entries, existing):
        mark_duplicate_entries(entries, existing, self.base_account)
        entries.extend(extract_out_of_place(existing, entries, self.base_account))

def sanitize_assets(ledger):
    for tid, entry in ledger.items():
        if entry['asset'] in assets:
            entry['asset'] = assets[entry['asset']]
        yield tid, entry

def group_ledgers(ledger):
    groups = {}
    for tid, entry in sanitize_assets(ledger):
        time = entry['time']
        if time not in groups:
            groups[time] = []
        groups[time].append(entry)
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

def extract_postings(group, base_account, base_currency, pnl_account):
    postings = []
    if is_sale(group, base_currency):
        postings.append(Posting(pnl_account, None, None, None, None, None))
    for ledger in group:
        asset_account = base_account + ':' + ledger['asset']
        amount = Decimal(ledger['amount'])
        # Ignore 0-amount KFEE entries
        if ledger['asset'] == 'KFEE' and amount < 0.01:
            continue
        cost = extract_cost(ledger, group, base_currency)
        amount = Amount(amount, ledger['asset'])
        postings.append(Posting(asset_account, amount, cost, None, None, None))
    return postings

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
