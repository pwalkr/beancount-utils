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
    # Prefix used to denote raw CUSIP commodities (bonds, no ticker)
    bond_prefix = 'C.'
    fid = '7776'

    def __init__(self, account_map, currency='USD'):
        """
        account_map: dict mapping OFX account_id -> Beancount base account name
        Example:
          {
              '12345678': 'Assets:Investments:Fidelity:HSA',
              '87654321': 'Assets:Investments:BrokerA:RothIRA'
          }
        """
        self.account_map = account_map
        self.currency = currency

    def identify(self, filepath):
        mimetype, encoding = mimetypes.guess_type(filepath)
        if mimetype != 'application/vnd.intu.qfx' and not filepath.lower().endswith('.ofx'):
            return False
        parser = OFXTree()
        parser.parse(filepath)
        ofx = parser.convert()
        return ofx.signon.fi.fid == self.fid

    def account(self, _):
        return "Assets:Fidelity"

    def extract(self, filepath, existing):
        entries = []
        parser = OFXTree()
        parser.parse(filepath)
        ofx = parser.convert()
        self._set_tickers(ofx)

        for stmt in ofx.statements:
            account_id = stmt.account.acctid
            base_account = self.account_map.get(account_id)
            if not base_account:
                print(f"Unknown account id {account_id}, skipping statement")
                continue  # Unknown account, skip

            for txn in stmt.transactions:
                if type(txn) is model.BUYSTOCK:
                    entries.extend(self._handle_buy_stock(txn, base_account))
                elif type(txn) is model.SELLSTOCK:
                    entries.extend(self._handle_sell_stock(txn, base_account))
                elif type(txn) is model.INCOME:
                    entries.extend(self._handle_income(txn, base_account))
                elif type(txn) is model.INVBANKTRAN:
                    entries.extend(self._handle_transfer(txn, base_account))
                else:
                    print(f"Skipping unsupported transaction type: {type(txn)}")

        return entries

    def _extract_date(self, txn):
        # dtposted is required, dttrade is prefferred if available
        return txn.dttrade.date() if hasattr(txn, 'dttrade') else txn.dtposted.date()

    def _get_full_account(self, base_account, ticker):
        return f"{base_account}:{ticker}"

    def _get_generic_meta(self, extraMeta=None):
        return new_metadata("", 0, extraMeta)

    def _get_income_account(self, base_account, ticker):
        return f"{base_account}:{ticker}".replace("Assets", "Income")

    def _get_ticker(self, txn):
        if txn.secid.uniqueid in self.tickers:
            return self.tickers[txn.secid.uniqueid]
        raise ValueError(f"Ticker for {txn.secid.uniqueid} missing from {self.tickers}")

    def _handle_buy_stock(self, txn, base_account):
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
                        self._get_full_account(base_account, self.currency),
                        Amount(txn.total.normalize(), self.currency),
                        None,
                        None, None, self._get_generic_meta()),
                    Posting(
                        self._get_full_account(base_account, ticker),
                        Amount(txn.units.normalize(), ticker),
                        Cost(txn.unitprice.normalize(), self.currency, None, None),
                        None, None, self._get_generic_meta()),
                ]
            )
        ]

    def _handle_sell_stock(self, txn, base_account):
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
                        self._get_full_account(base_account, ticker),
                        Amount(txn.units.normalize(), ticker),
                        CostSpec(None, None, None, None, None, None),
                        Amount(txn.unitprice.normalize(), self.currency),
                        None, self._get_generic_meta()),
                    Posting(
                        self._get_full_account(base_account, self.currency),
                        Amount(txn.total.normalize(), self.currency),
                        None,
                        None, None, self._get_generic_meta()),
                ]
            )
        ]

    def _handle_income(self, txn, base_account):
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
                        self._get_income_account(base_account, ticker),
                        None, None,
                        None,
                        None, self._get_generic_meta()),
                    Posting(
                        self._get_full_account(base_account, self.currency),
                        Amount(txn.total.normalize(), self.currency),
                        None, None,
                        None, self._get_generic_meta()),
                ]
            )
        ]

    def _handle_transfer(self, txn, base_account):
        print(f"Handling transfer: {txn.__repr__()}")
        meta = self._get_generic_meta({"memo": txn.memo})
        # meta = self._get_generic_meta({"raw": txn.__repr__()})  # DEBUG
        return [
            Transaction(
                meta=meta,
                date=self._extract_date(txn),
                flag='*',
                payee=None,
                narration=txn.name,
                tags=frozenset(),
                links=frozenset(),
                postings=[
                    Posting(
                        account=self._get_full_account(base_account, self.currency),
                        units=Amount(txn.trnamt.normalize(), self.currency),
                        meta=None, cost=None, price=None, flag=None
                    )
                ]
            )
        ]

    def _set_tickers(self, ofx):
        self.tickers = {}
        for security in ofx.securities:
            # Bond tickers are just CUSIP, invalid beancount commodities - add prefix
            if re.match('[0-9]', security.ticker):
                self.tickers[security.secid.uniqueid] = self.bond_prefix+security.ticker
            else:
                self.tickers[security.secid.uniqueid] = security.ticker
