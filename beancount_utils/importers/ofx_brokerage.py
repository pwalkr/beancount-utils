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


class CommodityResolver():
    def __init__(self, to_commodity, to_leaf):
        self.to_commodity = to_commodity
        self.to_leaf = to_leaf
        self.commodities = {}
        self.leafs = {}

    def load_securities(self, ofx_securities):
        for security in ofx_securities:
            self.leafs[security.secid.uniqueid] = self.to_leaf(security.ticker)
            self.commodities[security.secid.uniqueid] = self.to_commodity(security.ticker)

    def commodity(self, position):
        return self.commodities[position.secid.uniqueid]

    def leaf(self, position):
        return self.leafs[position.secid.uniqueid]


class Importer(beangulp.Importer):
    """An importer for brokerage statements."""

    def __init__(self, base_account, currency, match_fid, cash_leaf=None, div_account="Income:Dividends", fee_account="Expenses:Financial:Fees", int_account="Income:Interest", bond_per_x=100, to_commodity=None, pnl_account="Income:PnL", open_on_buy_debt=True, to_leaf=lambda ticker: ticker):
        self.base_account = base_account
        self.currency = currency
        self.match_fid = match_fid
        self.to_leaf = to_leaf
        # Bond tickers are just CUSIP, invalid beancount commodities - add prefix
        self.to_commodity = to_commodity if to_commodity else lambda ticker : "C."+ticker if re.match('[0-9]', ticker) else ticker
        self.cash_account = self.full_account(cash_leaf if cash_leaf else currency)
        self.div_account = div_account
        self.fee_account = fee_account
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
        return self.base_account

    def extract(self, filepath, existing):
        entries = []
        parser = OFXTree()
        parser.parse(filepath)
        ofx = parser.convert()

        cr = CommodityResolver(self.to_commodity, self.to_leaf)
        cr.load_securities(ofx.securities)

        for security in ofx.securities:
            entries.append(self.extract_security_price(security, cr))

        for stmt in ofx.statements:
            asofdate = stmt.dtasof.date()
            for invpos in stmt.invposlist:
                self.extract_position_balance(invpos, cr, entries)

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
                    self.extract_buydebt(txn, cr, postings)
                    if self.open_on_buy_debt:
                        bact = self.full_account(cr.leaf(txn))
                        entries.append(Open(self.generic_meta(), tdate, bact, None, None))
                elif type(txn) is model.BUYSTOCK:
                    ticker = cr.commodity(txn)
                    camt = Amount(txn.total, self.currency)
                    pamt = Amount(txn.units, ticker)
                    pcost = Cost(txn.unitprice, self.currency, None, None)
                    postings.append(Posting(self.cash_account, camt, None, None, None, None))
                    postings.append(Posting(self.full_account(cr.leaf(txn)), pamt, pcost, None, None, self.generic_meta()))
                elif type(txn) is model.INCOME:
                    pamt = Amount(Decimal(txn.total), self.currency)
                    if "Interest" in txn.memo:
                        postings.append(Posting(self.int_account, -pamt, None, None, None, None))
                    elif "Dividend" in txn.memo:
                        postings.append(Posting(self.div_account, -pamt, None, None, None, None))
                    else:
                        raise Exception("Unknown transaction {}".format(txn))
                    postings.append(Posting(self.cash_account, pamt, None, None, None, self.generic_meta()))
                elif type(txn) is model.INVBANKTRAN:
                    pamt = Amount(Decimal(txn.trnamt), self.currency)
                    postings.append(Posting(self.cash_account, pamt, None, None, None, self.generic_meta()))
                elif type(txn) is model.SELLSTOCK:
                    self.extract_sellstock(txn, cr, postings)
                elif type(txn) is model.TRANSFER:
                    #if "Dividend" in txn.memo # TODO: handle stock split
                    ticker = cr.commodity(txn)
                    pamt = Amount(Decimal(txn.units), ticker)
                    pact = self.full_account(cr.leaf(txn))
                    postings.append(Posting(pact, pamt, None, None, None, self.generic_meta()))

                entries.append(Transaction(tmeta, tdate, '*', None, narr, frozenset(), frozenset(), postings))
        return entries

    def extract_buydebt(self, transaction, cr, postings):
        # From cash account
        postings.append(Posting(self.cash_account, Amount(transaction.total, self.currency), None, None, None, None))
        # To commodity account
        account = self.full_account(cr.leaf(transaction))
        pamt = Amount(transaction.units/self.bond_per_x, cr.commodity(transaction))
        pcost = Cost(transaction.unitprice, self.currency, None, None)
        postings.append(Posting(account, pamt, pcost, None, None, self.generic_meta()))

    def extract_position_balance(self, position, cr, entries):
        # TODO handle POSDEBT
        if type(position) is POSSTOCK:
            account = self.full_account(cr.leaf(position))
            amount = Amount(position.units, cr.commodity(position))
            date = position.dtpriceasof.date()
            entries.append(Balance(self.generic_meta(), date, account, amount, None, None))

    def extract_security_price(self, security, cr):
        ticker = cr.commodity(security)
        date = security.dtasof.date()
        amount = Amount(security.unitprice, self.currency)
        return Price(self.generic_meta(), date, ticker, amount)

    def extract_sellstock(self, transaction, cr, postings):
        # PnL to absorb difference between lot cost basis and proceeds
        postings.append(Posting(self.pnl_account, None, None, None, None, None))
        # From commodity account
        amount = Amount(Decimal(transaction.units), cr.commodity(transaction))
        cost = CostSpec(None, None, None, None, None, None)
        price = Amount(transaction.unitprice, self.currency)
        account = self.full_account(cr.leaf(transaction))
        postings.append(Posting(account, amount, cost, price, None, self.generic_meta()))
        # To cash account
        postings.append(Posting(self.cash_account, Amount(transaction.total, self.currency), None, None, None, None))


    def full_account(self, leaf):
        if leaf:
            return "{}:{}".format(self.base_account, leaf)
        else:
            # Using monolithic non-leafed account
            return self.base_account

    def generic_meta(self):
        return new_metadata(None, None)

    def deduplicate(self, entries, existing):
        mark_duplicate_entries(entries, existing, self.base_account)
        entries.extend(extract_out_of_place(existing, entries, self.base_account))
