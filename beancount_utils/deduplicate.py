import datetime
from beancount.core import data
from beangulp.extract import DUPLICATE


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


def wrap_postings(entries, account):
    return [
        PostingWrapper(posting, entry)
        for entry in data.filter_txns(entries)
        for posting in entry.postings if posting.account == account
    ]


class PostingWrapper():
    def __init__(self, posting, entry):
        self.posting = posting
        self.entry = entry
        self.date = entry.date
        self.amount = posting.units.number
        self._match = None

    def match(self, ip, window):
        if self._match is not None or ip._match is not None:
            return False

        if abs(self.date - ip.date) <= window:
            if self.amount == ip.amount:
                self._match = ip
                ip._match = self
                return True

        return False
