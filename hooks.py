#!/usr/bin/env python3

from utils import citi

from beancount.core.data import Posting, Transaction, Amount
from beangulp.extract import DUPLICATE
from beangulp.similar import comparator
import datetime
import re
import yaml


class Decorator:
    payables = []

    def __init__(self, config_yaml, exclude=None):
        self.config_yaml = config_yaml
        self.exclude = (lambda x:False) if exclude is None else exclude

    def prime(self):
        with open(self.config_yaml, 'r') as file:
            self.payables = yaml.safe_load(file).get('payables')

    def hook(self, extracts, existing):
        for filename, entries, account, importer in extracts:
            yield (filename, self.decorate_all(entries), account, importer)

    def decorate_all(self, entries):
        for entry in entries:
            if not self.exclude(entry):
                entry = self.decorate(entry)
            yield entry

    def decorate(self, entry):
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
                        entry = entry._replace(tags=entry.tags.union(payable['tags']))
        return entry


OUT_OF_PLACE = 'OUT_OF_PLACE'


def outofplace(extracts, existing):
    for filename, entries, account, importer in extracts:
        entries = entries + list(_outofplace(entries, existing, account, importer))
        yield (filename, entries, account, importer)


def _outofplace(extracted, existing, account, importer):
    window = datetime.timedelta(days=2)
    date_start, date_end = outofplace_dates(extracted)
    cmp = comparator(window, 0.05)

    for entry in outofplace_timely(date_start, date_end, existing, account):
        for incoming in extracted:
            if cmp(incoming, entry):
                break
        else:
            yield entry._replace(tags=entry.tags.union({OUT_OF_PLACE}))


def outofplace_dates(extracted):
    date_start = None
    date_end = None
    for entry in extracted:
        if date_start == None or entry.date < date_start:
            date_start = entry.date
        if date_end == None or entry.date > date_end:
            date_end = entry.date
    if date_start == None or date_end == None:
        raise ValueError('Failed to find dates in extracted entries')
    return date_start, date_end


def outofplace_timely(date_start, date_end, existing, account):
    for entry in existing:
        if isinstance(entry, Transaction):
            if entry.date >= date_start and entry.date <= date_end:
                for posting in entry.postings:
                    if posting.account == account:
                        yield entry
                        break


def prune_dupes(extracts, existing):
    for filename, entries, account, importer in extracts:
        yield (filename, _prune_dupes(entries), account, importer)


def _prune_dupes(entries):
    for entry in entries:
        if not isinstance(entry, Transaction) or not DUPLICATE in entry.meta:
            yield entry
