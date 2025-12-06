from datetime import datetime
from decimal import Decimal
from os import path
from beancount.core.data import Amount, Balance, Posting, Price, Transaction, new_metadata
from beangulp import importer, mimetypes
import json
import re

from beancount_utils.deduplicate import mark_duplicate_entries, extract_out_of_place
from beancount_utils.decorator import Decorator


class Importer(importer.Importer):
    def __init__(self, account, acctid, currency='USD', cash_leaf=None, income_account="Income:Investments:Merrill:{commodity}", fee_account="Expenses:Financial:Fees", decorate=None, decorator: Decorator = None):
        self._account = account
        self.acctid = acctid
        self.currency = currency
        self.cash_leaf = cash_leaf if cash_leaf else currency
        self.income_account = income_account
        self.fee_account = fee_account
        self.decorate = decorate
        self.decorator = decorator

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'application/json':
            return False
        with open(filepath) as fd:
            head = fd.read(1024)
        return f'"id": "{self.acctid}"' in  head

    def account(self, filepath):
        return 'SimpleFIN'

    def extract(self, filepath, existing):
        data = self.load_json(filepath)
        for account in data['accounts']:
            if account['id'] == self.acctid:
                return self.extract_transactions(filepath, account) + self.extract_balances(filepath, account) + self.extract_prices(filepath, account)

    def load_json(self, filepath):
        with open(filepath) as f:
            return json.load(f)

    def extract_transactions(self, filepath, data):
        entries = []
        for transaction in data['transactions']:
            meta = new_metadata(filepath, 0)
            date = transaction['transacted_at'] if 'transacted_at' in transaction else transaction['posted']
            date = datetime.fromtimestamp(date).date()
            flag = '!' if 'pending' in transaction and transaction['pending'] else '*'
            payee = transaction['payee'] if 'payee' in transaction else transaction['description']
            # Many of these have absurdly     long                 spaces
            narration = re.sub(' +', ' ', transaction['description'])
            amount = Amount(Decimal(transaction['amount']), self.currency)
            postings = [Posting(self._account, amount, None, None, None, {'description':narration})]
            entries.append(Transaction(meta, date, flag, payee, narration, frozenset(), frozenset(), postings))
        return entries

    def extract_balances(self, filepath, data):
        date = datetime.fromtimestamp(data['balance-date']).date()
        entries = []
        for holding in data['holdings']:
            if holding['symbol']:
                commodity = holding['symbol']
                leaf = commodity
            elif holding['description'] == "ML DIRECT DEPOSIT PROGRM":
                commodity = self.currency
                leaf = self.cash_leaf
            else:  # some bonds don't have a symbol or CUSIP
                # stderr...
                continue
            meta = new_metadata(filepath, 0)
            account = f"{self._account}:{leaf}"
            amount = Amount(Decimal(holding['shares']), commodity)
            entries.append(Balance(meta, date, account, amount, None, None))
        return entries

    def extract_prices(self, filepath, data):
        date = datetime.fromtimestamp(data['balance-date']).date()
        entries = []
        for holding in data['holdings']:
            if holding['symbol']:
                commodity = holding['symbol']
            else:
                continue
            meta = new_metadata(filepath, 0)
            total = Decimal(holding['market_value'])
            shares = Decimal(holding['shares'])
            per_share = total / shares
            price = Amount(Decimal(per_share), self.currency)
            entries.append(Price(meta, date, commodity, price))
        return entries

    def deduplicate(self, entries, existing):
        mark_duplicate_entries(entries, existing, self._account)
        entries.extend(extract_out_of_place(existing, entries, self._account))
        # Decorate after marking duplicates so extra target postings don't interfere
        if self.decorate:
            self.decorate(entries)
        if self.decorator:
            self.decorator.decorate(entries)
