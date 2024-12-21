from datetime import datetime
from decimal import Decimal
from os import path
from beancount.core.data import Amount, Posting, Transaction, new_metadata
from beangulp import importer, mimetypes
from beangulp.importers import csvbase
import json
import re
import sys


class Importer(importer.Importer):
    def __init__(self, accounts, currency='USD', decorate=None):
        self.accounts = accounts
        self.currency = currency
        self.decorate = decorate

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'application/json':
            return False
        with open(filepath) as f:
            data = json.load(f)
            if 'accounts' not in data or not isinstance(data['accounts'], list) or 'id' not in data['accounts'][0]:
                return False
        return True

    def account(self, filepath):
        return 'SimpleFIN'

    def extract(self, filepath, existing):
        entries = []

        data = self.load_json(filepath)

        for account in data['accounts']:
            if account['id'] in self.accounts:
                self.extract_account(filepath, self.accounts[account['id']], account, entries)
            else:
                print("No input account for {} at {}".format(account['name'], account['org']['name']), file=sys.stderr)

        return entries

    def load_json(self, filepath):
        with open(filepath) as f:
            return json.load(f)

    def extract_account(self, filepath, account, data, entries):
        for transaction in data['transactions']:
<<<<<<< HEAD
            meta = new_metadata(filepath, 0)
=======
            meta = new_metadata(filepath, 0, {'memo':transaction['description']})
>>>>>>> 68feba8 (importers: add simplefin.py)
            date = transaction['transacted_at'] if 'transacted_at' in transaction else transaction['posted']
            date = datetime.fromtimestamp(date).date()
            flag = '!' if 'pending' in transaction and transaction['pending'] else '*'
            payee = transaction['payee'] if 'payee' in transaction else transaction['description']
            narration = transaction['description']
            amount = Amount(Decimal(transaction['amount']), self.currency)
<<<<<<< HEAD
            postings = [Posting(account, amount, None, None, None, {'description':transaction['description']})]
=======
            postings = [Posting(account, amount, None, None, None, None)]
>>>>>>> 68feba8 (importers: add simplefin.py)
            entries.append(Transaction(meta, date, flag, payee, narration, frozenset(), frozenset(), postings))

    def deduplicate(self, entries, existing):
        super().deduplicate(entries, existing)
        # Decorate after marking duplicates so extra target postings don't interfere
        if self.decorate:
            self.decorate(entries)
