import datetime
import json
import os
import re

from decimal import Decimal

import beangulp

from beancount.core.data import Amount, Balance, new_metadata


default_assets_map = {
    "XETH": "ETH",
    "XLTC": "LTC",
    "XXBT": "BTC",
    "XXDG": "DOGE",
    "XXLM": "XLM",
    "XXRP": "XRP",
    "ZUSD": "USD",
}


class Importer(beangulp.Importer):
    """An importer for Kraken Ledger JSON Export."""

    def __init__(self, base_account, assets_map=default_assets_map):
        self.base_account = base_account
        self.assets_map = assets_map

    def identify(self, filepath):
        if not filepath.lower().endswith(".json"):
            return False

        with open(filepath) as f:
            data = json.load(f)

        if not data or "result" not in data or not isinstance(data['result'], dict):
            return False

        # Check if result looks like currency:balance
        for asset, balance in data['result'].items():
            if asset in self.assets_map and bool(re.match(r'^-?\d+(\.\d+)?$', balance)):
                return True
        return False

    def account(self, filepath):
        return self.base_account

    def extract(self, filepath, existing):
        entries = []

        mod_time = os.path.getmtime(filepath)
        date = datetime.datetime.fromtimestamp(mod_time).date()

        with open(filepath) as f:
            data = json.load(f)

        combined = self.combine_stakes(data['result'])
        for asset in sorted(combined.keys()):
            balance = combined[asset]

            meta = new_metadata(filepath, 0)
            account = f"{self.base_account}:{asset}"
            amount = Amount(Decimal(balance), asset)
            entries.append(Balance(meta, date, account, amount, None, None))
        return entries

    def combine_stakes(self, raw_balances):
        """Combine .F, .S, balances into a single entry."""
        combined_balances = {}
        for asset, balance in raw_balances.items():
            asset = asset.split('.')[0]
            if asset in self.assets_map:
                asset = self.assets_map[asset]
            else:
                asset = asset
            if asset not in combined_balances:
                combined_balances[asset] = Decimal(balance)
            else:
                combined_balances[asset] += Decimal(balance)
        return combined_balances
