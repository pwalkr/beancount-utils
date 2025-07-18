from collections import namedtuple
from datetime import datetime
from decimal import Decimal
from os import path
from beancount.core import data
from beangulp import importer, mimetypes
from beangulp.importers import csvbase
import csv
import re


ClaimInfo = namedtuple('ClaimInfo', ['provider', 'patient'])


class Importer(importer.Importer):
    def __init__(self, claim_acount, currency, insurance_account=None, decorate=None, import_zero=False):
        self.claim_acount = claim_acount
        self.currency = currency
        self.insurance_account = insurance_account
        self.decorate = decorate
        self.import_zero = import_zero

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'text/csv':
            return False
        with open(filepath) as fd:
            head = fd.read(1024)
        return head.startswith('Claim Number,Patient Name,Date Visited')

    def account(self, filepath):
        return None

    def extract(self, filepath, existing):
        entries = []
        rc = re.compile('[$,)]')
        with open(filepath) as csvfile:
            for entry in csv.DictReader(csvfile):
                flag = '*'
                date = datetime.strptime(entry['Date Visited'], '%Y-%m-%d')
                year = date.strftime('%Y')
                account = self.claim_acount(ClaimInfo(
                    provider=entry['Visited Provider'],
                    patient=entry['Patient Name'],
                ))
                payee = entry['Visited Provider']
                narration = entry['Patient Name']
                amount = rc.sub('', entry['Your Responsibility'])
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

    def deduplicate(self, entries, existing):
        super().deduplicate(entries, existing)
        # Decorate after marking duplicates so extra target postings don't interfere
        if self.decorate:
            self.decorate(entries)
