#!/usr/bin/env python3

from utils import citi

from beancount.core.data import Posting, Transaction, Amount
from beangulp.extract import DUPLICATE
import re
import yaml


class Decorator:
    payables = []

    def __init__(self, config_yaml):
        self.config_yaml = config_yaml

    def prime(self):
        with open(self.config_yaml, 'r') as file:
            self.payables = yaml.safe_load(file).get('payables')

    def hook(self, extracts, existing):
        for filename, entries, account, importer in extracts:
            yield (filename, self.decorate(entries), account, importer)

    def decorate(self, entries):
        for entry in entries:
            if isinstance(entry, Transaction):
                for payable in self.payables:
                    if re.search(payable['re'], entry.payee, flags=re.IGNORECASE):
                        if 'expense_account' in payable:
                            entry.postings.append(Posting(
                                payable['expense_account'],
                                -entry.postings[0].units,
                                None, None, None, None))
                        if 'narration' in payable:
                            entry = entry._replace(narration=payable['narration'])
                        if 'payee' in payable:
                            entry = entry._replace(payee=payable['payee'])
                        if 'tags' in payable:
                            entry = entry._replace(tags=payable['tags'])
                        break
            yield entry


def prune_dupes(extracts, existing):
    for filename, entries, account, importer in extracts:
        yield (filename, _prune_dupes(entries), account, importer)


def _prune_dupes(entries):
    for entry in entries:
        if not isinstance(entry, Transaction) or not DUPLICATE in entry.meta:
            yield entry
