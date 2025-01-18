import re
import yaml
from beancount.core.data import Posting, Transaction


def load_yaml(filepath):
    with open(filepath, 'r') as file:
        return yaml.safe_load(file)


class Decorator:
    def __init__(self, decorations, exclude=None):
        self.decorations = decorations
        self.exclude = (lambda x: False) if exclude is None else exclude

    @classmethod
    def from_yaml(cls, filepath):
        decorations = load_yaml(filepath)
        return cls.from_list(decorations)

    @classmethod
    def from_list(cls, decorations):
        return cls([Decoration(decoration) for decoration in decorations])

    def append_yaml(self, filepath):
        self.append_list(load_yaml(filepath))

    def append_list(self, decorations):
        self.decorations = self.decorations + decorations

    def decorate(self, entries):
        for idx, entry in enumerate(entries):
            if isinstance(entry, Transaction) and not self.exclude(entry):
                entries[idx] = self.decorate_transaction(entry)

    def decorate_transaction(self, transaction):
        for decoration in self.decorations:
            if decoration.match(transaction):
                return decoration.decorate(transaction)
        return transaction


class Decoration:
    def __init__(self, decoration):
        if 're' not in decoration:
            raise ValueError("Decoration config must include 're' Regex field for matching payees.")
        self.re = decoration['re']
        self.rec = re.compile(self.re, flags=re.IGNORECASE)

        # Optional fields for decoration
        self.flag = decoration.get('flag')
        self.narration = decoration.get('narration')
        self.payee = decoration.get('payee')
        self.tags = decoration.get('tags')
        self.target_account = decoration.get('target_account')

    def match(self, transaction):
        if not transaction.payee:
            return False
        return self.rec.search(transaction.payee) is not None

    def decorate(self, transaction):
        if self.target_account:
            transaction.postings.append(
                Posting(self.target_account, -transaction.postings[0].units, None, None, None, None)
            )
        if self.flag:
            transaction = transaction._replace(flag=self.flag)
        if self.narration:
            transaction = transaction._replace(narration=self.narration)
        if self.payee:
            transaction = transaction._replace(payee=self.payee)
        if self.tags:
            transaction = transaction._replace(tags=transaction.tags.union(self.tags))

        return transaction
