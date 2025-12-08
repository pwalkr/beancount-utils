from collections import namedtuple
from datetime import datetime
from decimal import Decimal
from os import path
from beancount.core import data
from beangulp import importer, mimetypes
from beangulp.importers import csvbase
import csv
import re

from beancount_utils.deduplicate import mark_duplicate_entries


# Used by leaf_account function to create a unique account name
ClaimInfo = namedtuple('ClaimInfo', ['provider', 'patient', 'year'])


class Importer(importer.Importer):
    def __init__(self, currency, base_account=None, leaf_account=None, insurance_account=None, decorate=None, import_zero=False):
        self.base_account = base_account
        self.leaf_account = leaf_account
        self.currency = currency
        self.insurance_account = insurance_account
        self.decorate = decorate
        self.import_zero = import_zero
        # For fixing amounts
        self.fixrc = re.compile('[$,)]')
        # Keep track for deduplication
        self.found_accounts = set()

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'text/csv':
            return False
        with open(filepath) as fd:
            head = fd.read(1024)
        return head.startswith('"Service Date","Patient","Provider","Status","Billed","Plan Paid","Patient Responsibility","I Owe","My Payments"')

    def account(self, filepath):
        return None

    def extract(self, filepath, existing):
        entries = []
        with open(filepath) as csvfile:
            for entry in csv.DictReader(csvfile):
                flag = '*'
                date = datetime.strptime(entry['Date Visited'], '%Y-%m-%d')
                account = self.full_account(date, entry)
                self.found_accounts.add(account)
                payee = entry['Visited Provider']
                narration = entry['Patient Name']
                amount = self.fix_amount(entry['Your Responsibility'])
                amount = amount.replace('(', '-')
                if amount:
                    amount = round(-Decimal(amount), 2)
                else:
                    amount = Decimal(0)
                units = data.Amount(amount, self.currency)

                meta = data.new_metadata(filepath, 0, {
                    'claim': entry['Claim Number'].strip(),
                    'provider': payee
                })

                if amount.__abs__() >= 0.01 or self.import_zero:
                    postings = [data.Posting(account, units, None, None, None, None)]

                    entries.append(data.Transaction(meta, date.date(), flag,
                                   payee, narration, frozenset(), frozenset(), postings))

                if self.insurance_account:
                    insamt = rc.sub('', entry["Amount Billed"])
                    insamt = insamt.replace('(', '-')
                    insamt = round(-Decimal(insamt), 2)
                    insamt = insamt - amount
                    insamt = data.Amount(insamt, self.currency)
                    inspost = [data.Posting(self.insurance_account, insamt, None, None, None, None)]
                    entries.append(data.Transaction(dict(meta), date.date(), flag,
                                   payee, narration, frozenset(), frozenset(), inspost))

        return entries

    def fix_amount(self, amount):
        return self.fixrc.sub('', amount)

    def full_account(self, date, entry):
        parts = []

        if self.base_account:
            parts.append(self.base_account)

        if self.leaf_account:
            parts.append(self.leaf_account(ClaimInfo(
                patient=entry['Patient Name'],
                provider=entry['Visited Provider'],
                year=date.strftime('%Y'),
            )))

        return ':'.join(parts)

    def deduplicate(self, entries, existing):
        for account in self.found_accounts:
            mark_duplicate_entries(entries, existing, account)
        # Decorate after marking duplicates so extra target postings don't interfere
        if self.decorate:
            self.decorate(entries)
