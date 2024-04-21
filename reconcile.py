#!/usr/bin/env python3

"""Transaction Reconciler

Given a list of input transactions and a beancount file for context, this script
will reconcile, printing a list of new transactions from the input that should
be imported as well as any existing postings that do not appear in the input
(potentially attributed to the wrong account).
"""

import argparse
import textwrap
import re
import sys
from beancount import loader
from beancount.core.data import Transaction
from beancount.parser.printer import EntryPrinter


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("existing",
                        type=argparse.FileType('r'),
                        help="beancount file of existing transactions")
    parser.add_argument("incoming",
                        default=sys.stdin,
                        nargs=(None if sys.stdin.isatty() else '?'),
                        type=argparse.FileType('r'),
                        help="list of new transactions to reconcile (default stdin)")
    parser.add_argument("--account", help="account to reconcile")
    parser.add_argument('--include-subaccounts', action=argparse.BooleanOptionalAction,
                        help="include subaccounts when matching existing transaction postings")
    return parser.parse_args()


class PostCounter():
    date_start = None
    date_end = None
    new = []
    mismatched = []

    def __init__(self, incoming, existing, account, subaccounts):
        self.account = account
        self.existing = existing
        self.subaccounts = subaccounts
        self.incoming = incoming

    def digest(self):
        self.parse_incoming()
        self.parse_existing()

    def parse_incoming(self):
        for entry in self.incoming:
            if self.date_start == None or entry.date < self.date_start:
                self.date_start = entry.date
            if self.date_end == None or entry.date > self.date_end:
                self.date_end = entry.date
            for posting in entry.postings:
                if posting.account == self.account:
                    self.new.append({
                        'amount': posting.units.number,
                        'entry': entry})

    def parse_existing(self):
        for entry in self.existing:
            if isinstance(entry, Transaction):
                if entry.date >= self.date_start and entry.date <= self.date_end:
                    for posting in entry.postings:
                        if (self.subaccounts and re.match(self.account, posting.account) or
                            not self.subaccounts and posting.account == self.account):
                                found = False
                                for ing in self.new:
                                    if ing['amount'] == posting.units.number:
                                        self.new.remove(ing)
                                        found = True
                                        break
                                if not found:
                                    self.mismatched.append(entry)

    def report(self):
        newCount = 0
        mmCount = 0
        ep = EntryPrinter()
        if self.new:
            print("; Transactions to add:\n")
            unique = []
            for item in self.new:
                if not item['entry'] in unique:
                    unique.append(item['entry'])
                    print(ep(item['entry']))
                    newCount+=1
        if self.mismatched:
            print("; Mismatched transactions:\n")
            unique = []
            for entry in self.mismatched:
                if not entry in unique:
                    unique.append(entry)
                    print(textwrap.indent(ep(entry), '; '))
                    mmCount+=1
        if newCount or mmCount:
            print("; +{}\n; -{}\n; total: {}".format(newCount, mmCount, newCount | mmCount))


def parse_beans(stream):
    entries, errors, options = loader.load_string(stream.read())
    return entries


if __name__ == "__main__":
    args = parse_args()
    incoming = parse_beans(args.incoming)
    existing = parse_beans(args.existing)
    pc = PostCounter(incoming, existing, args.account, args.include_subaccounts)
    pc.digest()
    pc.report()
