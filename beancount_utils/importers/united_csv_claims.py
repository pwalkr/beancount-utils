from datetime import datetime
from decimal import Decimal
from os import path
from beancount.core import data
from beangulp import importer, mimetypes
from beangulp.importers import csvbase
import csv
import re


class Importer(importer.Importer):
    def __init__(self, base_account, currency, insurance_account=None, decorate=None, provider_leaf=None):
        self.base_account = base_account
        self.currency = currency
        self.insurance_account = insurance_account
        self.decorate = decorate
        self.provider_leaf = provider_leaf

    def identify(self, filepath):
        if not path.basename(filepath).startswith('MedicalClaimSummary'):
            return False
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'text/csv':
            return False
        with open(filepath) as fd:
            head = fd.read(1024)
        return head.startswith('Claim Number,Patient Name,Date Visited')

    def account(self, filepath):
        return self.base_account

    def extract(self, filepath, existing):
        entries = []
        rc = re.compile('[$,)]')
        with open(filepath) as csvfile:
            for entry in csv.DictReader(csvfile):
                flag = '*'
                date = datetime.strptime(entry['Date Visited'], '%Y-%m-%d')
                year = date.strftime('%Y')
                account = self.full_account({
                    "provider": entry['Visited Provider'],
                    "patient": entry['Visited Provider'],
                })
                payee = entry['Visited Provider']
                narration = entry['Patient Name']
                amount = rc.sub('', entry['Your Responsibility'])
                amount = amount.replace('(', '-')
                amount = round(-Decimal(amount), 2)
                units = data.Amount(amount, self.currency)

                meta = data.new_metadata(filepath, 0, {
                    'claim': entry['Claim Number'].strip(),
                    'provider': payee
                })

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

    def full_account(self, entry):
        if self.provider_leaf:
            leaf = self.provider_leaf(entry)
            return self.base_account + ':' + leaf
        else:
            return self.base_account

    def deduplicate(self, entries, existing):
        super().deduplicate(entries, existing)
        # Decorate after marking duplicates so extra target postings don't interfere
        if self.decorate:
            self.decorate(entries)
