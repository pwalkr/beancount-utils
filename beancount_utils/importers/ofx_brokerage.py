from datetime import datetime, timedelta
from decimal import Decimal
import re

from ofxtools.Parser import OFXTree
import ofxtools.models.invest.transactions as model
from ofxtools.models.invest.positions import POSDEBT, POSSTOCK

from beancount.core.data import Amount, Balance, Open, Posting, Price, Transaction, new_metadata
from beancount.core.position import Cost, CostSpec
import beangulp
from beangulp import mimetypes
from beangulp.testing import main

from beancount_utils.deduplicate import mark_duplicate_entries, extract_out_of_place


# Prefix used to denote raw CUSIP commodities (bonds, no ticker)
bond_prefix = 'C.'


class Importer(beangulp.Importer):
    """An importer for brokerage statements."""

    def __init__(self, base_account, currency, match_fid, cash_leaf=None, div_account="Income:Dividends", fee_account="Expenses:Financial:Fees", int_account="Income:Interest", bond_per_x=100, pnl_account="Income:PnL", open_on_buy_debt=True, file_account=None):
        self.base_account = base_account
        self.currency = currency
        self.match_fid = match_fid
        self.cash_account = self.full_account(cash_leaf if cash_leaf else currency)
        self.div_account = div_account
        self.fee_account = fee_account
        self.file_account = file_account
        self.int_account = int_account
        self.pnl_account = pnl_account
        self.bond_per_x = bond_per_x
        # Bonds are awkward to keep in the same account open just-in-time
        self.open_on_buy_debt = open_on_buy_debt

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'application/vnd.intu.qfx' and not filepath.lower().endswith('.ofx'):
            return False
        parser = OFXTree()
        parser.parse(filepath)
        ofx = parser.convert()
        return ofx.signon.fi.fid == self.match_fid

    def account(self, filepath):
        return self.file_account

    def get_ticker(self, txn):
        return self.tickers[txn.secid.uniqueid]

    def extract(self, filepath, existing):
        entries = []
        parser = OFXTree()
        parser.parse(filepath)
        ofx = parser.convert()

        self.extract_tickers(ofx.securities)

        for security in ofx.securities:
            entries.append(self.extract_security_price(security))

        for stmt in ofx.statements:
            print("\n\n{}\n\n".format(stmt.__repr__()))
            asofdate = stmt.dtasof.date()
            if 'invposlist' in stmt:
                for invpos in stmt.invposlist:
                    self.extract_position_balance(invpos, entries)

            for txn in stmt.transactions:
                tdate = txn.dttrade.date() if hasattr(txn, 'dttrade') else txn.dtposted.date()
                tmeta = new_metadata(filepath, 0, {"memo": txn.__repr__()})
                #tmeta = new_metadata(filepath, 0, {"type": type(txn).__name__})
                #tmeta = new_metadata(filepath, 0)
                narr = txn.name if hasattr(txn, 'name') else txn.memo
                postings = []

                if hasattr(txn, "fees") and txn.fees >= 0.01:
                    postings.append(Posting(self.fee_account, Amount(txn.fees, self.currency), None, None, None, None))

                # https://github.com/csingley/ofxtools/blob/master/ofxtools/models/invest/transactions.py
                if type(txn) is model.BUYDEBT:
                    self.extract_buydebt(txn, postings)
                    if self.open_on_buy_debt:
                        bact = self.full_account(self.get_ticker(txn))
                        entries.append(Open(self.generic_meta(filepath), tdate, bact, None, None))
                elif type(txn) is model.BUYSTOCK:
                    ticker = self.get_ticker(txn)
                    camt = Amount(txn.total, self.currency)
                    pamt = Amount(txn.units, ticker)
                    pcost = Cost(txn.unitprice, self.currency, None, None)
                    postings.append(Posting(self.cash_account, camt, None, None, None, None))
                    postings.append(Posting(self.full_account(ticker, pamt, pcost, None, None, self.generic_meta(filepath))))
                elif type(txn) is model.INCOME:
                    pamt = Amount(Decimal(txn.total), self.currency)
                    if "interest" in txn.memo.lower():
                        postings.append(Posting(self.int_account, -pamt, None, None, None, None))
                    elif "dividend" in txn.memo.lower():
                        postings.append(Posting(self.div_account, -pamt, None, None, None, None))
                    elif "cap gain" in txn.memo.lower():
                        pass
                    else:
                        raise Exception("Unknown transaction {}".format(txn))
                    postings.append(Posting(self.cash_account, pamt, None, None, None, self.generic_meta()))
                elif type(txn) is model.INVBANKTRAN:
                    pamt = Amount(Decimal(txn.trnamt), self.currency)
                    postings.append(Posting(self.cash_account, pamt, None, None, None, self.generic_meta()))
                elif type(txn) is model.SELLDEBT:
                    self.extract_selldebt(txn, postings)
                elif type(txn) is model.SELLSTOCK:
                    self.extract_sellstock(txn, postings)
                elif type(txn) is model.TRANSFER:
                    #if "Dividend" in txn.memo # TODO: handle stock split
                    ticker = self.get_ticker(txn)
                    pamt = Amount(Decimal(txn.units), ticker)
                    pact = self.full_account(self.get_ticker(txn))
                    postings.append(Posting(pact, pamt, None, None, None, self.generic_meta()))

                entries.append(Transaction(tmeta, tdate, '*', None, narr, frozenset(), frozenset(), postings))
        return entries

    def extract_buydebt(self, transaction, postings):
        # From cash account
        postings.append(Posting(self.cash_account, Amount(transaction.total, self.currency), None, None, None, None))
        # To commodity account
        account = self.full_account(self.get_ticker(transaction))
        amount = Amount(transaction.units/self.bond_per_x, self.get_ticker(transaction))
        pcost = Cost(transaction.unitprice, self.currency, None, None)
        postings.append(Posting(account, amount, pcost, None, None, self.generic_meta()))

    def extract_position_balance(self, position, entries):
        # TODO handle POSDEBT
        if type(position) is POSSTOCK:
            account = self.full_account(self.get_ticker(position))
            amount = Amount(position.units, self.get_ticker(position))
            date = position.dtpriceasof.date()
            entries.append(Balance(self.generic_meta(), date, account, amount, None, None))

    def extract_security_price(self, security):
        ticker = self.get_ticker(security)
        date = security.dtasof.date()
        amount = Amount(security.unitprice, self.currency)
        return Price(self.generic_meta(), date, ticker, amount)

    def extract_selldebt(self, transaction, postings):
        # PnL to absorb difference between lot cost basis and proceeds
        postings.append(Posting(self.pnl_account, None, None, None, None, None))
        # From commodity account
        amount = Amount(transaction.units/self.bond_per_x, self.get_ticker(transaction))
        cost = CostSpec(None, None, None, None, None, None)
        price = Amount(transaction.unitprice, self.currency)
        account = self.full_account(self.get_ticker(transaction))
        postings.append(Posting(account, amount, cost, price, None, self.generic_meta()))
        # To cash account
        postings.append(Posting(self.cash_account, Amount(transaction.total, self.currency), None, None, None, None))

    def extract_sellstock(self, transaction, postings):
        # PnL to absorb difference between lot cost basis and proceeds
        postings.append(Posting(self.pnl_account, None, None, None, None, None))
        # From commodity account
        amount = Amount(Decimal(transaction.units), self.get_ticker(transaction))
        cost = CostSpec(None, None, None, None, None, None)
        price = Amount(transaction.unitprice, self.currency)
        account = self.full_account(self.get_ticker(transaction))
        postings.append(Posting(account, amount, cost, price, None, self.generic_meta()))
        # To cash account
        postings.append(Posting(self.cash_account, Amount(transaction.total, self.currency), None, None, None, None))

    def extract_tickers(self, ofx_securities):
        """
        Extract map of cusip:ticker

        Bonds (and potentially other securities) have no alternative ticker. The
        CUSIP is not a valid beancount commodity (starting with a number) so these
        values are prefixed with "C."
        """

        self.tickers = {}
        for security in ofx_securities:
            # Bond tickers are just CUSIP, invalid beancount commodities - add prefix
            if re.match('[0-9]', security.ticker):
                self.tickers[security.secid.uniqueid] = bond_prefix+security.ticker
            else:
                self.tickers[security.secid.uniqueid] = security.ticker


    def full_account(self, leaf):
        if leaf:
            return "{}:{}".format(self.base_account, leaf.replace(bond_prefix,''))
        else:
            # Using monolithic non-leafed account
            return self.base_account

    def generic_meta(self, filepath=""):
        return new_metadata(filepath, 0)

    def deduplicate(self, entries, existing):
        mark_duplicate_entries(entries, existing, self.base_account)
        entries.extend(extract_out_of_place(existing, entries, self.base_account))
