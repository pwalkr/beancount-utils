from collections import namedtuple
from datetime import datetime
from decimal import Decimal
from os import path
from beancount.core.data import Amount, Balance, Posting, Price, Transaction, new_metadata
from beancount.core.position import Cost, CostSpec
from beangulp import importer, mimetypes
from beangulp.importers import csvbase
import csv
import re

from beancount_utils.deduplicate import mark_duplicate_entries


class Importer(importer.Importer):
    def __init__(self, asset_account='Assets:Merrill', currency='USD', income_account=None, div_account=None, decorator=None):
        self.asset_account = asset_account
        self.currency = currency
        self.income_account = income_account if income_account else asset_account.replace('Assets', 'Income')
        self.div_account = div_account if div_account else self.income_account
        self.decorator = decorator

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'text/csv':
            return False
        with open(filepath) as fd:
            head = fd.read(1024)
        # TODO: match "Account #"
        return head.startswith('"Trade Date","Settlement Date","Pending/Settled","Account Nickname"')

    def account(self, filepath):
        return None

    def extract(self, filepath, existing):
        entries = []
        with open(filepath) as csvfile:
            for entry in csv.DictReader(csvfile):
                try:
                    date = datetime.strptime(entry['Trade Date'], '%m/%d/%Y').date()
                    tx_type = entry['Description 1 ']
                    merrill_type = entry['Type']
                    if tx_type == 'Purchase ':
                        self.extract_purchase(date, merrill_type, entry, entries)
                    elif tx_type == 'Dividend':
                        self.extract_dividend(date, merrill_type, entry, entries)
                    elif tx_type == 'Funds Received':
                        self.extract_received(date, merrill_type, entry, entries)
                    else:
                        raise ValueError(f"Unknown transaction type: {tx_type}")
                except Exception as e:
                    print(f"Error processing entry {entry}: {e}")
                    raise e
        return entries

    def extract_dividend(self, date, merrill_type, entry, entries):
        narration = entry['Description 2']
        entries.append(Importer.Transaction(date, narration,
            meta={
                'description': entry['Description 2'],
                'merrill_type': merrill_type,
            },
            postings=[
                Importer.Posting(
                    self.render_account(self.div_account, entry['Symbol/CUSIP #']),
                    Amount(-Decimal(entry['Amount ($)']), self.currency),
                ),
                Importer.Posting(
                    self.render_account(self.asset_account, self.currency),
                    Amount(Decimal(entry['Amount ($)']), self.currency),
                ),
            ]
        ))

    def extract_purchase(self, date, merrill_type, entry, entries):
        total_cost = Decimal(re.sub(r"\(([\d.,]+)\)", r"-\1", entry['Amount ($)']))
        narration = entry['Description 2']
        entries.append(Importer.Transaction(date, narration,
            meta={
                'description': entry['Description 2'],
                'merrill_type': merrill_type,
            },
            postings=[
                Importer.Posting(
                    self.render_account(self.asset_account, self.currency),
                    Amount(total_cost, self.currency),
                ),
                Importer.Posting(
                    self.render_account(self.asset_account, entry['Symbol/CUSIP #']),
                    Amount(Decimal(entry['Quantity']), entry['Symbol/CUSIP #']),
                    cost=CostSpec(None, total_cost, self.currency, None, None, None),
                ),
            ]
        ))

    def extract_received(self, date, merrill_type, entry, entries):
        entries.append(Importer.Transaction(date, 'Transfer', [
            Importer.Posting(
                self.render_account(self.asset_account, self.currency),
                Amount(Decimal(entry['Amount ($)']), self.currency),
                meta={
                    'description': entry['Description 2'],
                    'merrill_type': merrill_type,
                },
            ),
        ]))

    def render_account(self, account, commodity):
        return account.format(commodity=commodity)

    @staticmethod
    def Posting(account, units, cost=None, price=None, meta=None):
        return Posting(
            flag=None,
            account=account,
            units=units,
            cost=cost,
            price=price,
            meta=new_metadata('', 0, meta) if meta else None,
        )

    @staticmethod
    def Transaction(date, narration, postings, meta=None):
        return Transaction(
            meta=new_metadata('', 0, meta),
            date=date,
            flag='*',
            payee=None,
            narration=narration,
            tags=frozenset(),
            links=frozenset(),
            postings=postings,
        )

    def deduplicate(self, entries, existing):
        super().deduplicate(entries, existing)
        if self.decorator:
            self.decorator.decorate(entries)
