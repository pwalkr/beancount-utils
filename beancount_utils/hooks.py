from beancount.core.data import Transaction
from beangulp.extract import DUPLICATE


def prune_dupes(extracts, existing):
    for filename, entries, account, importer in extracts:
        yield (filename, yield_non_dupes(entries), account, importer)


def yield_non_dupes(entries):
    for entry in entries:
        if not isinstance(entry, Transaction) or not DUPLICATE in entry.meta:
            yield entry
