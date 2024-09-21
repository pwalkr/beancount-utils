#!/usr/bin/env python3

"""Digest and reconcile incoming transactions.

Given a target account, this performs posting-level deduplication and marks
existing transactions unmatched if they do not appear in the incoming list.
Transactions are decorated according to an input config yaml of the form:

    payables:
    - payee: ACME Corp
      re: ACME CORP MISC 156843513
      account: 'Expenses:Misc'
"""

import argparse
import datetime
import re
import sys
import yaml
from beancount import loader
from beancount.core.data import Posting, Transaction
from beancount.parser import parser
from beangulp import extract
from beangulp.extract import DUPLICATE


POST_DUP_META = 'duplicate'


def parse_args():
    """Parse command-line arguments."""
    ap = argparse.ArgumentParser(description="Reconcile and decorate transactions.")
    ap.add_argument("existing", help="beancount file of existing transactions")
    ap.add_argument("incoming", help="list of new transactions to reconcile (- for stdin)")
    ap.add_argument("--account", help="report out-of-place transactions for account")
    ap.add_argument('--decorate', type=argparse.FileType('r'), help="configure decorations (yaml)")
    ap.add_argument('--dupes', default=False, action=argparse.BooleanOptionalAction)
    return ap.parse_args()


def load_yaml(file):
    """Load YAML configuration."""
    try:
        return yaml.safe_load(file) if file else {}
    except yaml.YAMLError as e:
        print(f"Error loading YAML: {e}", file=sys.stderr)
        sys.exit(1)


def decorate(incoming, decorations):
    """Decorate incoming transactions with payables configuration."""
    payables = decorations.get('payables', [])
    for entry in incoming:
        if isinstance(entry, Transaction):
            for payable in payables:
                if re.search(payable['re'], entry.narration, flags=re.IGNORECASE):
                    entry = update_transaction(entry, payable)
                    break
        yield entry


def update_transaction(entry, payable):
    """Update a transaction with decoration information."""
    if 'account' in payable:
        entry.postings.append(Posting(payable['account'], None, None, None, None, None))
    if 'narration' in payable:
        entry = entry._replace(narration=payable['narration'])
    if 'payee' in payable:
        entry = entry._replace(payee=payable['payee'])
    if 'tags' in payable:
        entry = entry._replace(tags=entry.tags.union(payable['tags']))
    return entry


def mark_duplicate_transactions(incoming, existing, account, window):
    """Mark transactions that contain duplicate postings."""
    mark_duplicate_postings(incoming, existing, account, window)
    for entry in incoming:
        if isinstance(entry, Transaction):
            if any(POST_DUP_META in posting.meta for posting in entry.postings):
                entry.meta[DUPLICATE] = True


def mark_duplicate_postings(entries, context, account, window):
    """Mark postings that match existing ones in the given context."""
    context_postings = [
        {'posting': posting, 'entry': entry}
        for entry in context if isinstance(entry, Transaction)
        for posting in entry.postings if posting.account == account
    ]
    for entry in entries:
        if isinstance(entry, Transaction):
            for posting in entry.postings:
                if posting.account == account:
                    mark_posting_if_duplicate(posting, entry, context_postings, window)


def mark_posting_if_duplicate(posting, entry, context_postings, window):
    """Check if a posting is a duplicate and mark it."""
    for candidate in context_postings:
        if candidate['posting'].units.number == posting.units.number:
            if abs(candidate['entry'].date - entry.date) <= window:
                if 'match' not in candidate:
                    candidate['match'] = True
                    posting.meta[POST_DUP_META] = True
                    break


def get_mismatched_context(incoming, context, account, window):
    """Yield transactions from context that are unmatched."""
    mark_duplicate_postings(context, incoming, account, window)
    for entry in context:
        if isinstance(entry, Transaction):
            if any(posting.account == account and POST_DUP_META not in posting.meta for posting in entry.postings):
                yield entry._replace(tags=entry.tags.union(['_MISMATCHED_']), flag='!')


def existing_context(incoming, existing, account):
    """Filter existing transactions based on time of incoming."""
    incoming_sorted = sorted([ x for x in incoming if isinstance(x, Transaction)], key=lambda x: x.date)
    date_start, date_end = incoming_sorted[0].date, incoming_sorted[-1].date
    return (entry for entry in existing if isinstance(entry, Transaction) and date_start <= entry.date <= date_end)


def prune_dupes(entries):
    """Remove duplicate transactions."""
    return (entry for entry in entries if not isinstance(entry, Transaction) or DUPLICATE not in entry.meta)


def digest(incoming, existing, account, window, decorations):
    """Digest incoming transactions by marking duplicates, decorating, and reconciling with existing"""

    # Get relevant context of existing transactions
    context = list(existing_context(incoming, existing, account))

    # Mark duplicates in incoming transactions
    mark_duplicate_transactions(incoming, context, account, window)

    # Decorate incoming transactions
    incoming = list(decorate(incoming, decorations))

    # Add mismatched context to incoming transactions
    incoming.extend(get_mismatched_context(incoming, context, account, window))

    return incoming


def main():
    args = parse_args()

    # Load existing and incoming transactions
    existing = loader.load_file(args.existing)[0]
    incoming = parser.parse_file(args.incoming)[0]
    decorations = load_yaml(args.decorate)

    # Setup time window
    window = datetime.timedelta(days=2)

    incoming = digest(incoming, existing, args.account, window, decorations)

    # Optionally prune duplicates
    if not args.dupes:
        incoming = list(prune_dupes(incoming))

    # Output the reconciled transactions
    extract.print_extracted_entries([(args.incoming, incoming, None, None)], sys.stdout)


if __name__ == "__main__":
    main()
