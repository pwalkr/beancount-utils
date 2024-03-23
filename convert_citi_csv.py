#!/usr/bin/env python3

"""Citibank CSV Transaction Converter

This script converts transactions from a Citibank csv export into beancount
language syntax
"""

import argparse
import csv
import sys


parser = argparse.ArgumentParser()
parser.add_argument('input',
                    default=sys.stdin,
                    nargs=(None if sys.stdin.isatty() else '?'),
                    type=argparse.FileType('r'),
                    help="input csv (default stdin or '-')")
default_account = 'Liabilities:Credit:Citibank'
parser.add_argument('--account',
                    default=default_account,
                    help="name of posting account (default {})".format(default_account))
args = parser.parse_args()


txn_template = """\
{date} * "{description}"
  {account}  {amount} USD\
"""


for entry in csv.DictReader(args.input):
    if entry['Debit']:
        amount = '-' + entry['Debit']
    elif entry['Credit']:
        amount = entry['Credit'].replace('-', '')
    date = "{2}-{0}-{1}".format(*entry['Date'].split('/'))
    print(txn_template.format(
        account=args.account,
        date=date,
        description=entry['Description'],
        amount=amount)
        + '\n')
