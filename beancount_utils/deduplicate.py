import datetime
from beancount.core import data
from beangulp.extract import DUPLICATE


def extract_out_of_place(existing, entries, account, window=datetime.timedelta(days=2)):
    incoming_postings = wrap_postings(entries, account)
    context = list(yield_context(existing, entries, account))
    for posting in wrap_postings(context, account):
        for candidate in incoming_postings:
            if posting.match(candidate, window):
                # Mark similar to beangulp.extract.mark_duplicate_entries
                posting.entry.meta[DUPLICATE] = candidate.entry
                break
    return [
        entry._replace(tags=entry.tags.union({'OUT_OF_PLACE'}))
        for entry in context
        if not entry.meta.pop(DUPLICATE, False)
    ]


def yield_context(existing, entries, account):
    txns = list(data.filter_txns(entries))
    open_date = txns[0].date
    close_date = txns[-1].date
    for entry in data.filter_txns(existing):
        if entry.date >= open_date and entry.date <= close_date:
            for posting in entry.postings:
                if posting.account == account:
                    yield clone_transaction(entry)
                    break


def mark_duplicate_entries(entries, context, account, window=datetime.timedelta(days=2)):
    context_postings = wrap_postings(context, account)
    for posting in wrap_postings(entries, account):
        for candidate in context_postings:
            if posting.match(candidate, window):
                # Mark similar to beangulp.extract.mark_duplicate_entries
                posting.entry.meta[DUPLICATE] = candidate.entry
                # Update flag. Can't update tuple so replace based on index
                for x, p in enumerate(posting.entry.postings):
                    if p is posting.posting:
                        posting.entry.postings[x] = posting.posting._replace(flag='!')
                break


def clone_transaction(entry):
    postings = []
    for posting in entry.postings:
        postings.append(posting._replace())

    return data.Transaction(
            entry.meta.copy(),
            entry.date,
            entry.flag,
            entry.payee,
            entry.narration,
            entry.tags,
            entry.links,
            postings)


def wrap_postings(entries, account):
    return [
        PostingWrapper(posting, entry)
        for entry in data.filter_txns(entries)
        for posting in entry.postings if posting.account.startswith(account)
    ]


class PostingWrapper():
    def __init__(self, posting, entry):
        self.posting = posting
        self.entry = entry
        self.date = entry.date
        self.account = posting.account
        self.amount = posting.units.number
        self.currency = posting.units.currency
        self._match = None

    def match(self, ip, window):
        if self._match is not None or ip._match is not None:
            return False

        if abs(self.date - ip.date) <= window:
            if self.account == ip.account and self.amount == ip.amount and self.currency == ip.currency:
                self._match = ip
                ip._match = self
                return True

        return False
