from datetime import datetime, timedelta
from decimal import Decimal
import re

from ofxtools.Parser import OFXTree
import ofxtools.models.invest.transactions as model

from beancount.core.data import Amount, Balance, Open, Posting, Price, Transaction, new_metadata
from beancount.core.position import Cost, CostSpec
import beangulp
from beangulp import mimetypes
from beangulp.testing import main

from beancount_utils.deduplicate import mark_duplicate_entries, extract_out_of_place


class Importer(beangulp.Importer):
    """An importer for brokerage statements."""

    def __init__(self, base_account, currency, match_fid, cash_leaf=None, div_account="Income:Dividends", fee_account="Expenses:Financial:Fees", int_account="Income:Interest", bond_prefix="BOND.", pnl_account="Income:PnL"):
        self.base_account = base_account
        self.currency = currency
        self.match_fid = match_fid
        self.bond_prefix = bond_prefix
        self.cash_account = self.get_account(cash_leaf if cash_leaf else currency)
        self.div_account = div_account
        self.fee_account = fee_account
        self.int_account = int_account
        self.pnl_account = pnl_account

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'application/vnd.intu.qfx':
            return False
        parser = OFXTree()
        parser.parse(filepath)
        ofx = parser.convert()
        return ofx.signon.fi.fid == self.match_fid

    def account(self, filepath):
        return self.base_account

    def extract(self, filepath, existing):
        entries = []
        parser = OFXTree()
        parser.parse(filepath)
        ofx = parser.convert()

        tickers = {}
        for security in ofx.securities:
            date = security.dtasof.date()
            amount = Amount(security.unitprice, 'USD')
            tickers[security.secid.uniqueid] = security.ticker
            entries.append(Price(new_metadata(filepath, 0), date, security.ticker, amount))
            #print("{}\n".format(security))

        # Only handling one stmt for now
        stmt = ofx.statements[0]

        for txn in stmt.transactions:
            tdate = txn.dttrade.date() if hasattr(txn, 'dttrade') else txn.dtposted.date()
            tmeta = new_metadata(filepath, 0, {"memo": txn.__repr__()})
            #tmeta = new_metadata(filepath, 0, {"type": type(txn).__name__})
            #tmeta = new_metadata(filepath, 0)
            narr = txn.name if hasattr(txn, 'name') else txn.memo
            postings = []
            pmeta = {"memo": txn.memo}

            if hasattr(txn, "fees") and txn.fees >= 0.01:
                postings.append(Posting(self.fee_account, Amount(txn.fees, self.currency), None, None, None, None))

            # https://github.com/csingley/ofxtools/blob/master/ofxtools/models/invest/transactions.py
            if type(txn) is model.BUYDEBT:
                pact = self.get_account(txn.secid.uniqueid)
                pamt = Amount(Decimal(1), self.bond_prefix + txn.secid.uniqueid)
                pcost = Cost(-txn.total, self.currency, None, None)
                entries.append(Open(new_metadata(filepath, 0), tdate, pact, None, None))
                postings.append(Posting(self.cash_account, Amount(txn.total, self.currency), None, None, None, None))
                postings.append(Posting(pact, pamt, pcost, None, None, pmeta))
            elif type(txn) is model.BUYSTOCK:
                ticker = tickers[txn.secid.uniqueid]
                camt = Amount(txn.total, self.currency)
                pamt = Amount(txn.units, ticker)
                pcost = Cost(txn.unitprice, self.currency, None, None)
                postings.append(Posting(self.cash_account, camt, None, None, None, None))
                postings.append(Posting(self.get_account(ticker), pamt, pcost, None, None, pmeta))
            elif type(txn) is model.INCOME:
                pamt = Amount(Decimal(txn.total), self.currency)
                if "Interest" in txn.memo:
                    postings.append(Posting(self.int_account, -pamt, None, None, None, None))
                elif "Dividend" in txn.memo:
                    postings.append(Posting(self.div_account, -pamt, None, None, None, None))
                else:
                    raise Exception("Unknown transaction {}".format(txn))
                postings.append(Posting(self.cash_account, pamt, None, None, None, pmeta))
            elif type(txn) is model.INVBANKTRAN:
                pamt = Amount(Decimal(txn.trnamt), self.currency)
                postings.append(Posting(self.cash_account, pamt, None, None, None, pmeta))
            elif type(txn) is model.SELLSTOCK:
                ticker = tickers[txn.secid.uniqueid]
                pamt = Amount(Decimal(txn.units), ticker)
                pcost = CostSpec(None, None, None, None, None, None)
                price = Amount(txn.unitprice, self.currency)
                postings.append(Posting(self.pnl_account, None, None, None, None, None))
                postings.append(Posting(self.cash_account, Amount(txn.total, self.currency), None, None, None, None))
                postings.append(Posting(self.get_account(ticker), pamt, pcost, price, None, pmeta))
            elif type(txn) is model.TRANSFER:
                #if "Dividend" in txn.memo # TODO: handle stock split
                ticker = tickers[txn.secid.uniqueid]
                pamt = Amount(Decimal(txn.units), ticker)
                pact = self.get_account(ticker)
                postings.append(Posting(pact, pamt, None, None, None, pmeta))

            entries.append(Transaction(tmeta, tdate, '*', None, narr, frozenset(), frozenset(), postings))

        bdate = stmt.balances.ballist[0].dtasof.date()
        bact = self.get_account(self.currency)
        bamt = Amount(stmt.balances.availcash, self.currency)
        entries.append(Balance(new_metadata(filepath, 0), bdate, bact, bamt, None, None))

        return entries

    def get_account(self, commodity):
        return "{}:{}".format(self.base_account, commodity)

    def deduplicate(self, entries, existing):
        mark_duplicate_entries(entries, existing, self.base_account)
        entries.extend(extract_out_of_place(existing, entries, self.base_account))
