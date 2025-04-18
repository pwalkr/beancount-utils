import csv
from os import path
import sys
from itertools import islice
from beangulp import mimetypes
from beangulp.importers import csvbase

from beancount_utils.deduplicate import mark_duplicate_entries, extract_out_of_place


class Importer(csvbase.Importer):
    date = csvbase.Date('Datetime', '%Y-%m-%dT%H:%M:%S')
    # Used to locate documentational/non-transaction entries
    date_index = 2
    payee = csvbase.Columns('From')
    narration = csvbase.Columns('Note')
    amount = csvbase.Amount('Amount (total)', {
        r'\+ \$': '',
        r'- \$': '-',
        r'^$': '0',
        })
    skiplines = 2

    def read(self, filepath):
        with open(filepath, encoding=self.encoding) as fd:
            # Skip header lines.
            lines = islice(fd, self.skiplines, None)

            reader = csv.reader(lines, dialect=self.dialect)

            # Map column names to column indices.
            names = None
            if self.names:
                headers = next(reader, None)
                if headers is None:
                    raise IndexError("The input file does not contain an header line")
                names = {name.strip(): index for index, name in enumerate(headers)}

            # Construct a class with attribute accessors for the
            # configured columns that works similarly to a namedtuple.
            attrs = {}
            for name, column in self.columns.items():
                attrs[name] = property(column.getter(names))
            row = type("Row", (tuple,), attrs)

            # Return data rows.
            for x in reader:
                # Ignore documentation fields with empty date
                if x[self.date_index]:
                    yield row(x)

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
