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
parser.add_argument('--staked-income-account',
                    default='Income:Interest',
                    help="posting account to use for staking income")
parser.add_argument('--combine-staked', help='combine staked ".S" assets for simplified account structure', action=argparse.BooleanOptionalAction)
args = parser.parse_args()


txn_template = """\
{date} * "Kraken" "{narration}"{staked_income_posting}
  {account}  {amount} {commodity} {decoration}

{dateb} balance {account}  {balance} {commodity}
"""


# Balance database for joining staked + unstaked assets
bdb = {}


for entry in csv.DictReader(args.input):
    amount = float(entry['amount'])
    #balance = float(re.sub('\\.?0+$', '', entry['balance']))
    balance = float(entry['balance'])
    asset = entry['asset']
    commodity = re.sub('\\.S$', '', asset)
    commodity_staked = commodity + '.S'
    date = datetime.datetime.strptime(entry['time'], "%Y-%m-%d %H:%M:%S")
    fee = float(entry['fee'])

    if amount >= 0:
        amount += fee
    else:
        amount -= fee

    if args.combine_staked:
        # Store current balance in db before combining staked/unstaked balance
        bdb[asset] = balance
        balance = bdb.get(commodity, 0) + bdb.get(commodity_staked, 0)

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

    if commodity != entry['asset']:
        decoration += '\n  staked: "{}"'.format(entry['asset'])

    print(txn_template.format(
        account="{}:{}".format(args.account, asset if not args.combine_staked else commodity),
        amount=amount,
        balance=balance,
        commodity=commodity,
        date=date.strftime("%Y-%m-%d"),
        # Set balance at start of next day
        dateb=(date+datetime.timedelta(days=1)).strftime("%Y-%m-%d"),
        decoration=decoration,
        narration=narration,
        staked_income_posting = "\n  " + args.staked_income_account if entry['type'] == 'staking' else ""
        ))
