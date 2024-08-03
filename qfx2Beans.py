#!/usr/bin/env python3

"""First National Bank Transaction Converter

This script converts transactions from a First National Bank QFX export into
beancount language syntax (string output)
"""

import argparse
from ofxtools import OFXTree


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('input',
                        help="input qfx file")
    parser.add_argument('--account',
                        help="name of posting account")
    parser.add_argument('--flag',
                        default='*',
                        help="name of posting account")
    return parser.parse_args()


def parse_ofx(file):
    parser = OFXTree()
    parser.parse(file)
    return parser.convert()


bean_template = """\
{date} {flag} "{payee}"
  {account} {amount} USD
    memo: "{memo}"
"""
def get_bean_str(txn, account, flag):
    date = txn.dtposted.strftime("%Y-%m-%d")
    return bean_template.format(
        account=account,
        amount=txn.trnamt,
        date=date,
        flag=flag,
        memo=txn.memo,
        payee=txn.name)


def get_bal_str(ledgerbal, account):
    date = ledgerbal.dtasof.strftime("%Y-%m-%d")
    return "{} balance {}  {} USD".format(date, account, ledgerbal.balamt)


args = parse_args()
ofx = parse_ofx(args.input)
stmt = ofx.statements[0]

for txn in reversed(stmt.transactions):
    print(get_bean_str(txn, args.account, args.flag))

print(get_bal_str(stmt.ledgerbal, args.account))
