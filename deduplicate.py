#!/usr/bin/env python3

"""Transaction Deduplicator

This runs the beangulp deduplicator independently on an incoming set of
transactions.
"""

import argparse
from beancount import loader
from beancount.core.data import Transaction
from beancount.parser import parser
from beangulp import extract, similar
from beangulp.extract import DUPLICATE
import datetime
import sys


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("existing",
                        help="beancount file of existing transactions")
    ap.add_argument("incoming",
                        help="list of new transactions to reconcile (- for stdin)")
    ap.add_argument('--dupes', default=False, action=argparse.BooleanOptionalAction)
    return ap.parse_args()


def deduplicate(incoming, existing):
    window = datetime.timedelta(days=2)
    extract.mark_duplicate_entries(incoming, existing, window, similar.comparator())


def prune_dupes(entries):
    for entry in entries:
        if not isinstance(entry, Transaction) or not DUPLICATE in entry.meta:
            yield entry


if __name__ == "__main__":
    args = parse_args()
    # loader handles import
    existing = loader.load_file(args.existing)[0]
    # parser handles '-' for stdin
    incoming = parser.parse_file(args.incoming)[0]
    deduplicate(incoming, existing)
    if args.dupes == False:
        incoming = prune_dupes(incoming)
    extract.print_extracted_entries([(args.incoming, incoming, None, None)], sys.stdout)
