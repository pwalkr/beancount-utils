from datetime import datetime
from decimal import Decimal
from os import path
from beancount.core.data import Amount, Posting, Transaction, new_metadata
from beangulp import importer, mimetypes
import json
import re

from beancount_utils.deduplicate import mark_duplicate_entries, extract_out_of_place
from beancount_utils.decorator import Decorator


class Importer(importer.Importer):
    def __init__(self, account, acctid, currency='USD', decorate=None, decorator: Decorator = None):
        self._account = account
        self.acctid = acctid
        self.currency = currency
        self.decorate = decorate
        self.decorator = decorator

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'application/json':
            return False
        with open(filepath) as f:
            data = json.load(f)
            if 'accounts' in data and isinstance(data['accounts'], list):
                for account in data['accounts']:
                    if 'id' in account and account['id'] == self.acctid:
                        return True
        return False

    def account(self, filepath):
        return 'SimpleFIN'

    def extract(self, filepath, existing):
        data = self.load_json(filepath)
        for account in data['accounts']:
            if account['id'] == self.acctid:
                return self.extract_account(filepath, self._account, account)

    def load_json(self, filepath):
        with open(filepath) as f:
            return json.load(f)

    def extract_account(self, filepath, account, data):
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
            postings = [Posting(account, amount, None, None, None, {'description':narration})]
            entries.append(Transaction(meta, date, flag, payee, narration, frozenset(), frozenset(), postings))
        return entries

    def deduplicate(self, entries, existing):
        mark_duplicate_entries(entries, existing, self._account)
        entries.extend(extract_out_of_place(existing, entries, self._account))
        # Decorate after marking duplicates so extra target postings don't interfere
        if self.decorate:
            self.decorate(entries)
        if self.decorator:
            self.decorator.decorate(entries)
