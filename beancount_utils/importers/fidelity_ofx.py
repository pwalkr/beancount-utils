from datetime import datetime, timedelta
from decimal import Decimal
import re

from ofxtools.Parser import OFXTree
import ofxtools.models.invest.transactions as model
from ofxtools.models.invest.positions import POSDEBT, POSSTOCK

from beancount.core.data import Amount, Balance, Close, Open, Posting, Price, Transaction, new_metadata
from beancount.core.position import Cost, CostSpec
import beangulp
from beangulp import mimetypes

from beancount_utils.deduplicate import mark_duplicate_entries, extract_out_of_place

class Importer(beangulp.Importer):
    """
    beangulp importer for Fidelity OFX files
    """
    fid = '7776'

    def __init__(self, account, acctid, currency='USD', mmkt='SPAXX', foreign_tax='Expenses:Taxes:Foreign'):
        """
        account: Base account to use for all Fidelity accounts
        currency: Currency to use for cash accounts
        mmkt: Ticker to match for money market funds
        """
        self.base_account = account
        self.acctid = acctid
        self.currency = currency
        self.mmkt = mmkt
        self.foreign_tax = foreign_tax

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'application/vnd.intu.qfx' and not filepath.lower().endswith('.ofx'):
            return False
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            head = f.read(1000)
        return f"<FID>{self.fid}</FID>" in head and f"<ACCTID>{self.acctid}</ACCTID>" in head

    def account(self, filepath):
        return self.base_account

    def extract(self, filepath, existing):
        entries = []
        parser = OFXTree()
        parser.parse(filepath)
        ofx = parser.convert()
        self._set_tickers(ofx)

        for stmt in ofx.statements:
            if self.acctid != stmt.account.acctid:
                print(f"Unknown account id {stmt.account.acctid}, skipping statement")
                continue  # Unknown account, skip

            for txn in stmt.transactions:
                if type(txn) is model.BUYSTOCK:
                    entries.extend(self._handle_buy_stock(txn))
                elif type(txn) is model.SELLSTOCK:
                    entries.extend(self._handle_sell_stock(txn))
                elif type(txn) is model.INCOME:
                    entries.extend(self._handle_income(txn))
                elif type(txn) is model.INVBANKTRAN:
                    entries.extend(self._handle_transfer(txn))
                else:
                    print(f"Skipping unsupported transaction type: {type(txn)}")

        return entries

    def _extract_date(self, txn):
        # dtposted is required, dttrade is prefferred if available
        return txn.dttrade.date() if hasattr(txn, 'dttrade') else txn.dtposted.date()

    def _get_full_account(self, ticker):
        if ticker == self.mmkt:
            ticker = self.currency
        return f"{self.base_account}:{ticker}"

    def _get_generic_meta(self, extraMeta=None):
        return new_metadata("", 0, extraMeta)

    def _get_income_account(self, ticker):
        return self._get_full_account(ticker).replace("Assets", "Income")

    def _get_ticker(self, txn):
        if txn.secid.uniqueid in self.tickers:
            return self.tickers[txn.secid.uniqueid]
        raise ValueError(f"Ticker for {txn.secid.uniqueid} missing from {self.tickers}")

    def _handle_buy_stock(self, txn):
        ticker = self._get_ticker(txn)
        if txn.memo != 'YOU BOUGHT':
            # If this changes to something more interesting, highlight it
            meta = self._get_generic_meta({"memo": txn.memo})
        else:
            meta = self._get_generic_meta()
        # meta = self._get_generic_meta({"raw": txn.__repr__()})  # DEBUG
        return [
            Transaction(
                meta=meta,
                date=self._extract_date(txn),
                flag='*',
                payee=None,
                # Memo/name are too generic
                narration=f"Buy {ticker}",
                tags=frozenset(),
                links=frozenset(),
                postings=[
                    Posting(
                        self._get_full_account(self.currency),
                        Amount(txn.total.normalize(), self.currency),
                        None,
                        None, None, self._get_generic_meta()),
                    Posting(
                        self._get_full_account(ticker),
                        Amount(txn.units.normalize(), ticker),
                        Cost(txn.unitprice.normalize(), self.currency, None, None),
                        None, None, self._get_generic_meta()),
                ]
            )
        ]

    def _handle_sell_stock(self, txn):
        ticker = self._get_ticker(txn)
        if txn.memo != 'YOU SOLD':
            # If this changes to something more interesting, highlight it
            meta = self._get_generic_meta({"memo": txn.memo})
        else:
            meta = self._get_generic_meta()
        # meta = self._get_generic_meta({"transaction": txn.__repr__()})  # DEBUG
        return [
            Transaction(
                meta=meta,
                date=self._extract_date(txn),
                flag='*',
                payee=None,
                # Memo/name are too generic
                narration=f"Sell {ticker}",
                tags=frozenset(),
                links=frozenset(),
                postings=[
                    Posting(
                        self._get_full_account(ticker),
                        Amount(txn.units.normalize(), ticker),
                        CostSpec(None, None, None, None, None, None),
                        Amount(txn.unitprice.normalize(), self.currency),
                        None, self._get_generic_meta()),
                    Posting(
                        self._get_full_account(self.currency),
                        Amount(txn.total.normalize(), self.currency),
                        None,
                        None, None, self._get_generic_meta()),
                ]
            )
        ]

    def _handle_income(self, txn):
        ticker = self._get_ticker(txn)
        if txn.memo != 'DIVIDEND RECEIVED':
            # If this changes to something more interesting, highlight it
            meta = self._get_generic_meta({"memo": txn.memo})
        else:
            meta = self._get_generic_meta()
        # meta = self._get_generic_meta({"raw": txn.__repr__()})  # DEBUG
        narration = "Dividend" if txn.incometype == 'DIV' else txn.incometype
        return [
            Transaction(
                meta=meta,
                date=self._extract_date(txn),
                flag='*',
                payee=None,
                narration=f"{narration} - {ticker}",
                tags=frozenset(),
                links=frozenset(),
                postings=[
                    Posting(
                        self._get_income_account(ticker),
                        None, None,
                        None,
                        None, self._get_generic_meta()),
                    Posting(
                        self._get_full_account(self.currency),
                        Amount(txn.total.normalize(), self.currency),
                        None, None,
                        None, self._get_generic_meta()),
                ]
            )
        ]

    def _handle_transfer(self, txn):
        meta = self._get_generic_meta({"memo": txn.memo})
        # meta = self._get_generic_meta({"raw": txn.__repr__()})  # DEBUG
        postings = [
            Posting(
                account=self._get_full_account(self.currency),
                units=Amount(txn.trnamt.normalize(), self.currency),
                meta=None, cost=None, price=None, flag=None
            ),
        ]
        return [
            Transaction(
                meta=meta,
                date=self._extract_date(txn),
                flag='*',
                payee=None,
                narration=txn.name,
                tags=frozenset(),
                links=frozenset(),
                postings=postings,
            )
        ]

    def _set_tickers(self, ofx):
        self.tickers = { security.secid.uniqueid: security.ticker for security in ofx.securities }
        # for security in ofx.securities:
        #     self.tickers[security.secid.uniqueid] = security.ticker
