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
    ap.add_argument("--account",
                        help="report out-of-place transactions for account")
    ap.add_argument('--dupes', default=False, action=argparse.BooleanOptionalAction)
    return ap.parse_args()


def deduplicate(incoming, existing):
    window = datetime.timedelta(days=2)
    extract.mark_duplicate_entries(incoming, existing, window, similar.comparator())


OOP_TAG = 'OUT_OF_PLACE'


def out_of_place(incoming, existing, account):
    date_start, date_end = oop_dates(incoming)
    candidates = list(oop_candidates(date_start, date_end, existing, account))
    deduplicate(candidates, incoming)
    oop = []
    for candidate in candidates:
        if not DUPLICATE in candidate.meta:
            oop.append(candidate._replace(tags=candidate.tags.union({OOP_TAG})))
    return oop


def oop_dates(incoming):
    date_start = None
    date_end = None
    for entry in incoming:
        if date_start == None or entry.date < date_start:
            date_start = entry.date
        if date_end == None or entry.date > date_end:
            date_end = entry.date
    if date_start == None or date_end == None:
        raise ValueError('Failed to find dates in incoming entries')
    return date_start, date_end


def oop_candidates(date_start, date_end, existing, account):
    for entry in existing:
        if isinstance(entry, Transaction):
            if entry.date >= date_start and entry.date <= date_end:
                for posting in entry.postings:
                    if posting.account == account:
                        yield entry
                        break


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
    if args.account is not None:
        incoming.extend(out_of_place(incoming, existing, args.account))
    if args.dupes == False:
        incoming = list(prune_dupes(incoming))
    incoming.sort(key=lambda x:x.date)
    extract.print_extracted_entries([(args.incoming, incoming, None, None)], sys.stdout)
