#!/usr/bin/env python3

"""Wealthfront Cash PDF Transaction Converter

This script converts transactions from a Wealthfront (Green-dot) monthly
statement into beancount language syntax
"""

import argparse
from pypdf import PdfReader
import re
import sys
import yaml


parser = argparse.ArgumentParser()
parser.add_argument('input',
                    help="input pdf")
default_account = 'Assets:Liquid:Wealthfront'
parser.add_argument('--account',
                    help="name of posting account (default {})".format(default_account))
parser.add_argument('--config',
                    type=argparse.FileType('r'),
                    help="configuration file")
args = parser.parse_args()


txn_template = """\
{date} {flag} "{payee}" "{narration}"
  memo: "{memo}"
  {account}  {amount} USD\
"""


config = yaml.safe_load(args.config) if args.config else {}
account = args.account if args.account else config.get('account', default_account)
payables = config.get('payables',[])


reader = PdfReader(args.input)
parts = []

# Ignore header/footer per https://pypdf.readthedocs.io/en/stable/user/extract-text.html#example-1-ignore-header-and-footer
def visitor_body(text, cm, tm, font_dict, font_size):
    y = cm[5]
    if y > 50 and y < 720:
        parts.append(text)

for page in reader.pages:
    #page.extract_text(visitor_text=visitor_body)
    parts.append(page.extract_text())

text_body = "".join(parts)

#print(text_body)
#sys.exit(0)

txn_date = None
txn_payee = None
prev_line = None
for line in text_body.splitlines():
    if txn_date and txn_payee:
        parts = re.split(r'\s\$', line)
        if len(parts) == 2:
            txn = {}
            for payable in payables:
                if re.search(payable['re'], txn_payee, flags=re.IGNORECASE):
                    txn = payable

            if parts[0] == 'Debit-':
                amount = '-' + parts[1]
            elif parts[0] == 'Deposit+':
                amount = parts[1]
            else:
                raise Exception('Unexpected amount category: {}'.format(parts[0]))

            print(txn_template.format(
                account=account,
                amount=amount,
                date=txn_date,
                flag=txn.get('flag','*'),
                memo=txn_payee,
                narration=txn.get('narration',''),
                payee=txn.get('payee',txn_payee)))
            if 'expense_account' in txn:
                print('  {}'.format(txn['expense_account']))
            print()
        txn_date = None
        txn_payee = None
        prev_line = None
    else:
        mtc = re.search(r'^(\d\d)/(\d\d)/(\d\d\d\d) (.*)$', line)
        if mtc and not mtc.group(4).startswith("End of Day Settlement"):
            txn_date = "{}-{}-{}".format(mtc.group(3), mtc.group(1), mtc.group(2))
            txn_payee = mtc.group(4)
            prev_line = line
        else:
            txn_date = None
            txn_payee = None
