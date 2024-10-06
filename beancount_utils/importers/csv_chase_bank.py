from os import path
from beangulp import mimetypes
from beangulp.importers import csvbase


class Importer(csvbase.Importer):
    date = csvbase.Date('Post Date', '%m/%d/%Y')
    narration = csvbase.Columns('Description')
    amount = csvbase.Amount('Amount')

    def identify(self, filepath):
        if not path.basename(filepath).startswith('Chase'):
            return False
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'text/csv':
            return False
        with open(filepath) as fd:
            head = fd.read(1024)
        return head.startswith('Transaction Date,Post Date,Description,Category,Type,Amount')
