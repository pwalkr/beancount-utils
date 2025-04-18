from collections import defaultdict
from os import path
import sys
from beancount.core import data
from beangulp import mimetypes
from beangulp.importers import csvbase

from beancount_utils.deduplicate import mark_duplicate_entries, extract_out_of_place


class Importer(csvbase.Importer):
    date = csvbase.Date('Datetime', '%Y-%m-%dT%H:%M:%S')
    party_from = csvbase.Columns('From')
    party_to = csvbase.Columns('To')
    narration = csvbase.Columns('Note')
    amount = csvbase.Amount('Amount (total)', {
        r'\+ \$': '',
        r'- \$': '-',
        r'^$': '0',
        })
    skiplines = 2

    def extract(self, filepath, existing):
        entries = []
        balances = defaultdict(list)
        default_account = self.account(filepath)

        # Compute the line number of the first data line.
        offset = int(self.skiplines) + bool(self.names) + 1

        for lineno, row in enumerate(self.read(filepath), offset):
            # Skip empty lines.
            if not row:
                continue

            try:
                tag = getattr(row, "tag", None)
                tags = {tag} if tag else frozenset()

                link = getattr(row, "link", None)
                links = {link} if link else frozenset()

                # This looks like an exercise in defensive programming
                # gone too far, but we do not want to depend on any field
                # being defined other than the essential ones.
                flag = getattr(row, "flag", self.flag)
                account = getattr(row, "account", default_account)
                currency = getattr(row, "currency", self.currency)
                units = data.Amount(row.amount, currency)

                party_from = getattr(row, "party_from", None)
                party_to = getattr(row, "party_to", None)
                if not party_from:
                    continue
                # Set payee to "the other party"
                payee = party_to if row.amount < 0 else party_from

                # Create a transaction.
                txn = data.Transaction(
                    self.metadata(filepath, lineno, row),
                    row.date,
                    flag,
                    payee,
                    row.narration,
                    tags,
                    links,
                    [
                        data.Posting(account, units, None, None, None, None),
                    ],
                )

                # Apply user processing to the transaction.
                txn = self.finalize(txn, row)

            except Exception as ex:
                # Report input file location of processing errors. This could
                # use Exception.add_note() instead, but this is available only
                # with Python 3.11 and later.
                raise RuntimeError(
                    f"Error processing {filepath} line {lineno + 1} with values {row!r}"
                ) from ex

            # Allow finalize() to reject the row extracted from the current row.
            if txn is None:
                continue

            # Add the transaction to the output list.
            entries.append(txn)

            # Add balance to balances list.
            balance = getattr(row, "balance", None)
            if balance is not None:
                date = row.date + datetime.timedelta(days=1)
                units = data.Amount(balance, currency)
                meta = data.new_metadata(filepath, lineno)
                balances[currency].append(
                    data.Balance(meta, date, account, units, None, None)
                )

        if not entries:
            return []

        # Append balances.
        for currency, balances in balances.items():
            entries.append(balances[-1 if order is Order.ASCENDING else 0])

        return entries

    def identify(self, filepath):
        if not path.basename(filepath).startswith('Venmo'):
            return False
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'text/csv':
            return False
        with open(filepath) as fd:
            head = fd.read(1024)
        return head.startswith('Account Statement - ')

    def deduplicate(self, entries, existing):
        mark_duplicate_entries(entries, existing, self.importer_account)
        entries.extend(extract_out_of_place(existing, entries, self.importer_account))
