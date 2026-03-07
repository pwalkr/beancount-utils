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
import io

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
        # Match new format with "Trade Date" or old format with "Account #"
        return ('"Trade Date"' in head and '"Settlement Date"' in head) or head.startswith('"Trade Date","Settlement Date","Pending/Settled","Account Nickname"')

    def account(self, filepath):
        return None

    def extract(self, filepath, existing):
        entries = []
        with open(filepath) as csvfile:
            # Skip header lines until we find the CSV header
            for line in csvfile:
                if line.startswith('"Trade Date"'):
                    # Found the CSV header, create a new reader starting from this line
                    csv_content = line + csvfile.read()
                    csv_reader = csv.DictReader(io.StringIO(csv_content))
                    break
            else:
                # If we didn't find the header, raise an error
                raise ValueError("Could not find CSV header in file")
            
            for entry in csv_reader:
                # Normalize keys and values by stripping whitespace (CSV may have trailing spaces)
                entry = {k.strip(): v.strip() if isinstance(v, str) else v for k, v in entry.items()}
                
                # Skip empty rows
                if all(v == '' or v is None for v in entry.values()):
                    continue
                # Skip trailing summary
                if 'Total' in entry['Trade Date']:
                    continue
                try:
                    date = datetime.strptime(entry['Trade Date'], '%m/%d/%Y').date()
                    description = entry['Description']
                    
                    # Determine transaction type from description
                    if description.startswith('Purchase'):
                        self.extract_purchase(date, entry, entries)
                    elif 'Dividend' in description:
                        self.extract_dividend(date, entry, entries)
                    elif description.startswith('Funds Received'):
                        self.extract_received(date, entry, entries)
                    else:
                        # Skip unknown transaction types silently or log them
                        pass
                except Exception as e:
                    print(f"Error processing entry {entry}: {e}")
                    raise e
        return entries

    def extract_dividend(self, date, entry, entries):
        narration = entry['Description']
        amount_str = entry['Amount'].replace('$', '').replace(',', '')
        entries.append(Importer.Transaction(date, narration,
            meta={
                'description': entry['Description'],
            },
            postings=[
                Importer.Posting(
                    self.render_account(self.div_account, entry['Symbol/ CUSIP']),
                    Amount(-Decimal(amount_str), self.currency),
                ),
                Importer.Posting(
                    self.render_account(self.asset_account, self.currency),
                    Amount(Decimal(amount_str), self.currency),
                ),
            ]
        ))

    def extract_purchase(self, date, entry, entries):
        amount_str = entry['Amount'].replace('$', '').replace(',', '')
        total_cost = Decimal(re.sub(r"\(([\d.,]+)\)", r"-\1", amount_str))
        narration = entry['Description']
        entries.append(Importer.Transaction(date, narration,
            meta={
                'description': entry['Description'],
            },
            postings=[
                Importer.Posting(
                    self.render_account(self.asset_account, self.currency),
                    Amount(total_cost, self.currency),
                ),
                Importer.Posting(
                    self.render_account(self.asset_account, entry['Symbol/ CUSIP']),
                    Amount(Decimal(entry['Quantity']), entry['Symbol/ CUSIP']),
                    cost=CostSpec(None, -total_cost, self.currency, None, None, None),
                ),
            ]
        ))

    def extract_received(self, date, entry, entries):
        amount_str = entry['Amount'].replace('$', '').replace(',', '')
        entries.append(Importer.Transaction(date, 'Transfer', [
            Importer.Posting(
                self.render_account(self.asset_account, self.currency),
                Amount(Decimal(amount_str), self.currency),
                meta={
                    'description': entry['Description'],
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
