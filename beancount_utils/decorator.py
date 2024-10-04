import re
import yaml
from beancount.core.data import Posting, Transaction


class Decorator:
    def __init__(self, decorations, exclude=None):
        self.decorations = decorations
        self.exclude = (lambda x: False) if exclude is None else exclude

    @classmethod
    def from_yaml(cls, filepath):
        with open(filepath, 'r') as file:
            decorations = yaml.safe_load(file)
        return cls.from_list(decorations)

    @classmethod
    def from_list(cls, decorations):
        return cls([Decoration(decoration) for decoration in decorations])

    def decorate(self, entries):
        for idx, entry in enumerate(entries):
            if isinstance(entry, Transaction) and not self.exclude(entry):
                entries[idx] = self.decorate_transaction(entry)

    def decorate_transaction(self, transaction):
        for decoration in self.decorations:
            if decoration.match(transaction):
                transaction = decoration.decorate(transaction)
        return transaction


class Decoration:
    def __init__(self, decoration):
        # Required field for matching
        self.re = decoration['re']
        self.rec = re.compile(self.re, flags=re.IGNORECASE)

        # Optional fields for decoration
        self.narration = decoration.get('narration')
        self.payee = decoration.get('payee')
        self.tags = decoration.get('tags')
        self.target_account = decoration.get('target_account')

    def match(self, transaction):
        return self.rec.search(transaction.payee) is not None

    def decorate(self, transaction):
        if self.target_account:
            transaction.postings.append(
                Posting(self.target_account, -transaction.postings[0].units, None, None, None, None)
            )
        if self.narration:
            transaction = transaction._replace(narration=self.narration)
        if self.payee:
            transaction = transaction._replace(payee=self.payee)
        if self.tags:
            transaction = transaction._replace(tags=transaction.tags.union(self.tags))

        return transaction
