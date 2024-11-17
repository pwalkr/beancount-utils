from datetime import datetime, timedelta
from decimal import Decimal
import re

from pypdf import PdfReader

from beancount.core.data import Amount, Balance, new_metadata
import beangulp
from beangulp import mimetypes


def pdf_to_text(filename):
    pages = [ page.extract_text() for page in PdfReader(filename).pages ]
    return ''.join(pages)


class Importer(beangulp.Importer):
    """An importer for Merrill PDF statements."""

    def __init__(self, base_account, last4acct):
        self.base_account = base_account
        self.last4acct = last4acct

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'application/pdf':
            return False

        text = pdf_to_text(filepath)
        if text:
            return re.search('Account Number: [0-9A-Za-z-\s]+{}[^\w]'.format(self.last4acct), text) is not None

    def account(self, filepath):
        return self.base_account

    def extract(self, filepath, existing):
        entries = []
        text = pdf_to_text(filepath)
        print(text)
        #entries.append(self._extract_balance(filepath, text))
        return entries

    def _extract_balance(self, filepath, text):
        return Balance(
            meta=new_metadata(filepath, 0),
            date=self._extract_balance_date(text),
            account=self._account,
            amount=Amount(self._extract_balance_amount(text), self.currency),
            tolerance=None, diff_amount=None
        )

    def _extract_balance_amount(self, text):
        # New Balance: $123.45
        match = re.search(r"New Balance: \$([0-9.]+)", text)
        return -Decimal(match.group(1))

    def _extract_balance_date(self, text):
        # Opening/Closing Date 02/27/24 - 03/26/24
        match = re.search("Opening/Closing Date [0-9/]+ - ([0-9/]+)", text)
        close_date = datetime.strptime(match.group(1), '%m/%d/%y')
        # Balance assertion is at start of day. Assert balance the day after close
        return (close_date + timedelta(days=1)).date()
