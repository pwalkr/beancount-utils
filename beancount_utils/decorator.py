import re
from decimal import Decimal
import yaml
from beancount.core.data import Posting, Transaction


def load_yaml(filepath):
    with open(filepath, 'r') as file:
        return yaml.safe_load(file)


default_scope = 'default'


class Decorator:
    def __init__(self, decorations, exclude=None):
        self.decorations = decorations
        self.exclude = (lambda x: False) if exclude is None else exclude

    @classmethod
    def from_yaml(cls, filepath, scope=None):
        return cls.from_config(load_yaml(filepath), scope)

    @classmethod
    def from_config(cls, decorations, scope=None):
        """Build from a parsed config: a list, or a dict of scoped lists."""
        if decorations is None:
            # Empty YAML document
            return cls([])
        elif isinstance(decorations, dict):
            return cls.from_dict(decorations, scope)
        elif isinstance(decorations, list):
            if scope is not None:
                raise ValueError("Scope is not applicable for list of decorations.")
            return cls.from_list(decorations)
        raise ValueError("Decorations must be a list or a dictionary.")

    @classmethod
    def from_list(cls, decorations):
        if not isinstance(decorations, list):
            raise ValueError("Decorations must be a list.")
        return cls([d if isinstance(d, Decoration) else Decoration(d) for d in decorations])

    @classmethod
    def from_dict(cls, decorations, scope=None):
        if not isinstance(decorations, dict):
            raise ValueError("Decorations must be a dictionary.")
        if scope and scope not in decorations:
            raise ValueError(f"Scope '{scope}' not found in decorations.")
        # An empty YAML section parses as None
        scoped = decorations.get(scope) or []
        default = decorations.get(default_scope) or []
        return cls([Decoration(decoration) for decoration in (scoped + default)])

    def append_yaml(self, filepath, scope=None):
        self.decorations = self.decorations + self.from_yaml(filepath, scope).decorations

    def append_list(self, decorations):
        self.decorations = self.decorations + self.from_list(decorations).decorations

    def decorate(self, entries):
        for idx, entry in enumerate(entries):
            if isinstance(entry, Transaction) and not self.exclude(entry):
                entries[idx] = self.decorate_transaction(entry)

    def decorate_transaction(self, transaction):
        for decoration in self.decorations:
            if decoration.match(transaction):
                return decoration.decorate(transaction)
        return transaction


def compile_optional(pattern):
    return re.compile(pattern, flags=re.IGNORECASE) if pattern is not None else None


def decimal_optional(value):
    return Decimal(str(value)) if value is not None else None


class Decoration:
    matchers = ('re', 'narration_re', 'account_re', 'min_amount', 'max_amount')

    def __init__(self, decoration):
        if not any(key in decoration for key in self.matchers):
            raise ValueError(f"Decoration config must include a matcher: {', '.join(self.matchers)}.")
        # Matchers: every one given must match
        self.re = decoration.get('re')
        self.rec = compile_optional(self.re)
        self.narration_rec = compile_optional(decoration.get('narration_re'))
        self.account_rec = compile_optional(decoration.get('account_re'))
        # Inclusive bounds on the first (imported) posting's number
        self.min_amount = decimal_optional(decoration.get('min_amount'))
        self.max_amount = decimal_optional(decoration.get('max_amount'))

        # Optional fields for decoration
        self.flag = decoration.get('flag')
        self.narration = decoration.get('narration')
        self.payee = decoration.get('payee')
        self.tags = decoration.get('tags')
        if isinstance(self.tags, str):
            # A string would union as a set of characters
            self.tags = [self.tags]
        self.source_account = decoration.get('source_account')
        self.target_account = decoration.get('target_account')

    def match(self, transaction):
        if self.rec and not (transaction.payee and self.rec.search(transaction.payee)):
            return False
        if self.narration_rec and not (transaction.narration and self.narration_rec.search(transaction.narration)):
            return False
        if self.account_rec and not any(self.account_rec.search(p.account) for p in transaction.postings):
            return False
        if self.min_amount is not None or self.max_amount is not None:
            units = transaction.postings[0].units if transaction.postings else None
            if units is None or units.number is None:
                return False
            if self.min_amount is not None and units.number < self.min_amount:
                return False
            if self.max_amount is not None and units.number > self.max_amount:
                return False
        return True

    def decorate(self, transaction):
        if self.source_account:
            transaction.postings.append(
                Posting(self.source_account, None, None, None, None, None))
        if self.target_account:
            transaction.postings.append(
                Posting(self.target_account, -transaction.postings[0].units, None, None, None, None))
        if self.flag:
            transaction = transaction._replace(flag=self.flag)
        if self.narration:
            transaction = transaction._replace(narration=self.narration)
        if self.payee is not None:
            transaction = transaction._replace(payee=self.payee)
        if self.tags:
            transaction = transaction._replace(tags=transaction.tags.union(self.tags))

        return transaction
