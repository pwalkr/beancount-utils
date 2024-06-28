#!/usr/bin/env python3

"""Kraken CSV Trades Converter

This script converts transactions from a kraken trades export into beancount
language syntax
"""

import argparse
from beancount.core import amount, data, flags, position
from beancount.parser import printer
import csv
import datetime
from decimal import Decimal
import re
import sys


DEFAULT_BASE = 'Assets:Investments:Kraken'
DEFAULT_PNL = 'Income:PnL'


def strip_zero(amt):
    return re.sub('\\.?0+$', '', amt)


class Kraken2Beans():
    def __init__(self, base_account=DEFAULT_BASE, flag=flags.FLAG_OKAY, pnl=DEFAULT_PNL):
        self.base_account = base_account
        self.flag = flag
        self.pnl_account = pnl

    def parse_types(self, trade):
        trade['cost'] = Decimal(strip_zero(trade['cost']))
        trade['time'] = datetime.datetime.strptime(trade['time'], "%Y-%m-%d %H:%M:%S.%f")
        trade['vol'] = Decimal(strip_zero(trade['vol']))
        trade['price'] = Decimal(strip_zero(trade['price']))


    def trade2txn(self, trade):
        (commodity, currency) = trade['pair'].split('/')
        date = trade['time'].strftime("%Y-%m-%d")
        coact = self.base_account + ":" + commodity
        coamt = amount.Amount(trade['vol'], commodity)
        cuact = self.base_account + ":" + currency
        cuamt = amount.Amount(trade['cost'], currency)

        if trade['type'] == 'buy':
            narration = "Buy " + commodity
            cost = position.Cost(trade['price'], currency, None, None)
            postings = [
                data.Posting(coact, coamt, cost, None, None, None),
                data.Posting(cuact, -cuamt, None, None, None, None),
            ]
        elif trade['type'] == 'sell':
            narration = "Sell " + commodity
            cost = position.CostSpec(None, None, None, None, None, None)
            price = amount.Amount(trade['price'], currency)
            postings = [
                data.Posting(self.pnl_account, None, None, None, None, None),
                data.Posting(coact, -coamt, cost, price, None, None),
                data.Posting(cuact, cuamt, None, None, None, None),
            ]
        else:
            raise ValueError('Unexpected trade type "{}"'.format(trade['type']))

        return data.Transaction(None, date, self.flag, None, narration, None, None, postings)

    def csv2Txns(self, incoming):
        for trade in csv.DictReader(incoming):
            self.parse_types(trade)
            yield self.trade2txn(trade)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('input',
                        default=sys.stdin,
                        nargs=(None if sys.stdin.isatty() else '?'),
                        type=argparse.FileType('r'),
                        help="input csv (default stdin or '-')")
    parser.add_argument('--account',
                        default=DEFAULT_BASE,
                        help="name of base posting account (default {}:USD, :BTC, etc)".format(DEFAULT_BASE))
    return parser.parse_args()


if __name__ == "__main__":
    k2b = Kraken2Beans()
    args = parse_args()
    entries = list(k2b.csv2Txns(args.input))
    printer.print_entries(entries)
