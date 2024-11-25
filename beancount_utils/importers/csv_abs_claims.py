from datetime import datetime
from decimal import Decimal
from os import path
from beancount.core import data
from beangulp import importer, mimetypes
from beangulp.importers import csvbase
import csv
import re


class Importer(importer.Importer):
    def __init__(self, account, currency, insurance_account=None, decorate=None):
        self.importer_account = account
        self.currency = currency
        self.insurance_account = insurance_account
        self.decorate = decorate

    def identify(self, filepath):
        if not path.basename(filepath).startswith('ExportClaims'):
            return False
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'text/csv':
            return False
        with open(filepath) as fd:
            head = fd.read(1024)
        return head.startswith('"Claim Number","Patient Name","Service Date"')

    def account(self, filepath):
        return self.importer_account

    def extract(self, filepath, existing):
        entries = []
        rc = re.compile('[$,)]')
        with open(filepath) as csvfile:
            for entry in csv.DictReader(csvfile):
                flag = '!'
                date = datetime.strptime(entry['Service Date'], '%m/%d/%Y')
                year = date.strftime('%Y')
                account = self.importer_account.format(year=year)
                payee = entry['Provider']
                narration = entry['Patient Name']
                amount = rc.sub('', entry['My Responsibility'])
                amount = amount.replace('(', '-')
                amount = round(-Decimal(amount), 2)
                units = data.Amount(amount, self.currency)

                meta = data.new_metadata(filepath, 0, {
                    'claim': entry['Claim Number'],
                    'provider': payee
                })

                postings = [data.Posting(account, units, None, None, None, None)]

                entries.append(data.Transaction(meta, date.date(), flag,
                               payee, narration, frozenset(), frozenset(), postings))

                if self.insurance_account:
                    insamt = rc.sub('', entry["Total Charges"])
                    insamt = insamt.replace('(', '-')
                    insamt = round(-Decimal(insamt), 2)
                    insamt = insamt - amount
                    insamt = data.Amount(insamt, self.currency)
                    inspost = [data.Posting(self.insurance_account, insamt, None, None, None, None)]
                    entries.append(data.Transaction(dict(meta), date.date(), flag,
                                   payee, narration, frozenset(), frozenset(), inspost))

        return entries

    def deduplicate(self, entries, existing):
        super().deduplicate(entries, existing)
        # Decorate after marking duplicates so extra target postings don't interfere
        if self.decorate:
            self.decorate(entries)
