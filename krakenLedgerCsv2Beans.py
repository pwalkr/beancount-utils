#!/usr/bin/env python3

"""Kraken CSV Ledger Converter

This script converts transactions from a kraken ledger export into beancount
language syntax
"""

import argparse
import csv
import datetime
import re
import sys


parser = argparse.ArgumentParser()
parser.add_argument('input',
                    default=sys.stdin,
                    nargs=(None if sys.stdin.isatty() else '?'),
                    type=argparse.FileType('r'),
                    help="input csv (default stdin or '-')")
default_base = 'Assets:Investments:Kraken'
parser.add_argument('--account',
                    default=default_base,
                    help="name of base posting account (default {}:USD, :BTC, etc)".format(default_base))
parser.add_argument('--currency',
                    default='USD',
                    help="name of base currency")
args = parser.parse_args()


txn_template = """\
{date} * "Kraken" "{narration}"
  {account}  {amount} {commodity} {decoration}

{dateb} balance {account}  {balance} {commodity}
"""


for entry in csv.DictReader(args.input):
    amount = float(entry['amount'])
    fee = float(entry['fee'])
    if amount >= 0:
        amount += fee
    else:
        amount -= fee
    commodity = entry['asset']
    date = datetime.datetime.strptime(entry['time'], "%Y-%m-%d %H:%M:%S")

    if entry['type'] == 'trade':
        narration = "{} {}".format('Buy' if amount >= 0 else 'Sell', commodity)
    else:
        narration = "{} {}".format(entry['type'].capitalize(), commodity)

    if float(amount) >= 0:
        decoration = "{{0 # {0} {1}}}".format(entry['amountusd'], args.currency)
    else:
        decoration = "{}" + " @@ {} {}".format(entry['amountusd'].replace('-', ''), args.currency)

    if fee > 0:
        decoration += '\n  fee: {}'.format(fee)

    print(txn_template.format(
        account="{}:{}".format(args.account, commodity),
        amount=amount,
        # Remove trailing zeros: 0.00000000 to just 0
        balance=re.sub('\\.?0+$', '', entry['balance']),
        commodity=commodity,
        date=date.strftime("%Y-%m-%d"),
        # Set balance at start of next day
        dateb=(date+datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
        decoration=decoration,
        narration=narration
        ))
