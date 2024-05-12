#!/usr/bin/env python3

"""Bank of America CSV Transaction Converter

This script converts transactions from a Bank of America csv export into
beancount language syntax
"""

import argparse
import csv
import re
import sys
import yaml


parser = argparse.ArgumentParser()
parser.add_argument('input',
                    default=sys.stdin,
                    nargs=(None if sys.stdin.isatty() else '?'),
                    type=argparse.FileType('r'),
                    help="input csv (default stdin or '-')")
default_account = 'Assets:Checking:BofA'
parser.add_argument('--account',
                    help="name of posting account (default {})".format(default_account))
parser.add_argument('--config',
                    type=argparse.FileType('r'),
                    help="configuration file")
args = parser.parse_args()


txn_template = """\
{date} {flag} "{payee}" {narration}
  {account}  {amount} USD
    memo: "{memo}"\
"""


config = yaml.safe_load(args.config) if args.config else {}
account = args.account if args.account else config.get('account', default_account)
payables = config.get('payables',[])


fieldnames = ['Date','Description','Amount','Running Bal.']
# Skip over Description,,Summary Amt. balances for now.
skipping = True

for entry in csv.DictReader(args.input, fieldnames=fieldnames):
    if skipping:
        if entry['Date'] == 'Date':
            skipping = False
        continue

    date = "{2}-{0}-{1}".format(*entry['Date'].split('/'))

    txn = {}
    for payable in payables:
        if re.search(payable['re'], entry['Description'], flags=re.IGNORECASE):
            txn = payable

    narration='"{}"'.format(txn.get('narration',''))
    for tag in txn.get('tags', []):
        narration += ' #' + tag

    print(txn_template.format(
        account=account,
        amount=entry['Amount'],
        date=date,
        flag=txn.get('flag','*'),
        memo=entry['Description'],
        narration=narration,
        payee=txn.get('payee',entry['Description'])))
    if 'expense_account' in txn:
        print('  {}\n'.format(txn['expense_account']))
    else:
        print()
