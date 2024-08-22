#!/usr/bin/env python3

"""Venmo CSV Transaction Converter
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
default_account = 'Assets:Venmo'
parser.add_argument('--account',
                    help="name of posting account (default {})".format(default_account))
args = parser.parse_args()


txn_template = """\
{date} {flag} "{payee}" "{narration}"
  {account}  {amount}
    memo: "{narration}"
"""


fieldnames = ['None','ID','Datetime','Type','Status', 'Note', 'From', 'To', 'Amount total', 'Amount tip', 'Amount tax', 'Amount fee', 'Tax Rate', 'Tax Exempt', 'Funding Source', 'Destination', 'Beginning Balance', 'Ending Balance']
# Skip over Description,,Summary Amt. balances for now.
to_skip = 4
ending_balance = None
last_date = None

for entry in csv.DictReader(args.input, fieldnames=fieldnames):
    if to_skip >= 1:
        to_skip -= 1
        continue

    if not entry['ID']:
        ending_balance = re.sub(r'[$+ ]', '', entry['Ending Balance']) + ' USD'
        break

    if entry['Status'] == 'Canceled':
        continue

    amount = float(re.sub(r'[$+ ]', '', entry['Amount total']))

    if amount < 0:
        payee = entry['To']
    else:
        payee = entry['From']

    if amount < 0 and entry['Funding Source'] != 'Venmo balance':
        amount = '0 USD ; {} USD from {}'.format(amount, entry['Funding Source'])
    else:
        amount = str(amount) + ' USD'

    last_date = entry['Datetime'][:10]

    print(txn_template.format(
        account=args.account,
        amount=amount,
        date=last_date,
        flag='*',
        payee=payee,
        narration=entry['Note']))

if ending_balance:
    print('{} balance {} {}'.format(last_date, args.account, ending_balance))
