import datetime
from decimal import Decimal
from beancount.core import data
from beangulp.extract import DUPLICATE


def extract_out_of_place(existing, entries, account, window=datetime.timedelta(days=2), id_key=None):
    incoming_postings = wrap_postings(entries, account, id_key)
    context = list(yield_context(existing, entries, account))
    context_postings = wrap_postings(context, account, id_key)
    for posting, candidate in match_postings(context_postings, incoming_postings, window):
        # Mark similar to beangulp.extract.mark_duplicate_entries
        posting.entry.meta[DUPLICATE] = candidate.entry
    for posting in context_postings:
        if posting._match is None:
            # Update flag. Can't update tuple so replace based on index
            for x, p in enumerate(posting.entry.postings):
                if p is posting.posting:
                    posting.entry.postings[x] = posting.posting._replace(flag='!')
    return [
        entry._replace(tags=entry.tags.union({'OUT_OF_PLACE'}))
        for entry in context
        if not entry.meta.pop(DUPLICATE, False)
    ]


def comparator(account, logger=None, imported_ids=None, meta_key='import_id'):
    """Returns a beangulp.extract.mark_duplicate_entries compatible comparison method."""
    def cmp(entry1, entry2):
        if hasattr(entry1, 'postings') and hasattr(entry2, 'postings'):
            for p1 in entry1.postings:
                if p1.account == account and meta_key in p1.meta:
                    for p2 in entry2.postings:
                        if p2.account == account:
                            if p2.meta and meta_key in p2.meta:
                                if p1.meta[meta_key] == p2.meta[meta_key]:
                                    if p1.units.currency == p2.units.currency and abs(p1.units.number - p2.units.number) < 0.00001:
                                        return True
                                    elif logger:
                                        logger.warning(f"Sanity check failed: amounts differ for import_id {p1.meta[meta_key]} ({p1.units.number} vs {p2.units.number})")
                                elif logger and imported_ids and p2.meta[meta_key] not in imported_ids:
                                    logger.warning(f"Existing entry import_id not in imported ids: {entry2.date} {entry2.flag} {entry2.payee} {entry2.narration} ({p2.meta[meta_key]})")
                            elif logger:
                                logger.warning(f"Existing entry missing import_id: {entry2.date} {entry2.flag} {entry2.payee} {entry2.narration}")
        return False
    return cmp


def warn_duplicate_import_id(account, existing, logger, meta_key='import_id'):
    """Log a warning if any import_id is encountered more than once in the existing entries."""
    found_import_ids = {}
    for entry in existing:
        if hasattr(entry, 'postings'):
            for posting in entry.postings:
                if posting.account == account and posting.meta and meta_key in posting.meta:
                    if posting.meta[meta_key] in found_import_ids:
                        logger.warning(f"Duplicate {meta_key} encountered: {posting.meta[meta_key]}")
                    found_import_ids[posting.meta[meta_key]] = True


def yield_context(existing, entries, account):
    txns = list(data.filter_txns(entries))
    if not txns:
        return
    open_date = txns[0].date
    close_date = txns[-1].date
    for entry in data.filter_txns(existing):
        if entry.date >= open_date and entry.date <= close_date:
            for posting in entry.postings:
                if posting.account == account:
                    yield clone_transaction(entry)
                    break


def mark_duplicate_entries(entries, context, account, window=datetime.timedelta(days=2), id_key=None):
    """Mark duplicate entries.

    When id_key is given, postings whose meta both carry that key match
    only on equal ids; postings missing it fall back to amount and date.
    """
    mark_duplicate_open_close(entries, context)
    mark_duplicate_postings(entries, context, account, window, id_key)
    mark_duplicate_prices(entries, context)


def mark_duplicate_open_close(entries, context):
    for entry in entries:
        if isinstance(entry, data.Open) or isinstance(entry, data.Close):
            for candidate in context:
                if type(entry) == type(candidate) and entry.date == candidate.date:
                    # Mark similar to beangulp.extract.mark_duplicate_entries
                    entry.meta[DUPLICATE] = candidate
                    break


def mark_duplicate_postings(entries, context, account, window=datetime.timedelta(days=2), id_key=None):
    postings = wrap_postings(entries, account, id_key)
    context_postings = wrap_postings(context, account, id_key)
    for posting, candidate in match_postings(postings, context_postings, window):
        # Mark similar to beangulp.extract.mark_duplicate_entries
        posting.entry.meta[DUPLICATE] = candidate.entry
        # Update flag. Can't update tuple so replace based on index
        for x, p in enumerate(posting.entry.postings):
            if p is posting.posting:
                posting.entry.postings[x] = posting.posting._replace(flag='!')


def match_postings(postings, candidates, window):
    """Pair postings one-to-one, taking exact id matches before fuzzy ones.

    The exact pass runs over every posting before any fuzzy matching, so an
    id-less posting earlier in either list cannot claim, by amount, the
    counterpart of a later posting with a matching id. A posting is yielded
    at most once: match() refuses postings already claimed.
    """
    for exact in (True, False):
        for posting in postings:
            for candidate in candidates:
                if posting.match(candidate, window, exact):
                    yield posting, candidate
                    break


def mark_duplicate_balances(entries, context, window=datetime.timedelta(days=2), tolerance=Decimal('0.0001')):
    for entry in entries:
        if isinstance(entry, data.Balance):
            for candidate in context:
                if (isinstance(candidate, data.Balance)
                        and entry.account == candidate.account
                        and abs(entry.date - candidate.date) <= window
                        and entry.amount.currency == candidate.amount.currency
                        and abs(entry.amount.number - candidate.amount.number) <= tolerance):
                    # Mark similar to beangulp.extract.mark_duplicate_entries
                    entry.meta[DUPLICATE] = candidate
                    break


def mark_duplicate_prices(entries, context):
    for entry in entries:
        if isinstance(entry, data.Price):
            for candidate in context:
                if isinstance(candidate, data.Price) and entry.date == candidate.date and entry.currency == candidate.currency:
                    if entry.amount == candidate.amount:
                        # Mark similar to beangulp.extract.mark_duplicate_entries
                        entry.meta[DUPLICATE] = candidate
                    else:
                        entry.meta['duplicate-price-error'] = f"Different amount than {candidate}"
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


def wrap_postings(entries, account, id_key=None):
    return [
        PostingWrapper(posting, entry, id_key)
        for entry in data.filter_txns(entries)
        for posting in entry.postings if posting.account.startswith(account)
    ]


class PostingWrapper():
    def __init__(self, posting, entry, id_key=None):
        self.posting = posting
        self.entry = entry
        self.date = entry.date
        self.account = posting.account
        self.amount = posting.units.number if posting.units is not None else None
        self.currency = posting.units.currency if posting.units is not None else None
        self.id = posting.meta.get(id_key) if id_key and posting.meta else None
        self._match = None

    def match(self, ip, window, exact=False):
        """Claim ip if it matches.

        With exact, only equal ids match. Otherwise ids must not both be
        present (they would have decided already) and amount, currency and
        date within window must agree.
        """
        if self._match is not None or ip._match is not None:
            return False

        # Match leaf accounts
        if not (self.account.startswith(ip.account) or ip.account.startswith(self.account)):
            return False

        if exact:
            matched = self.id is not None and self.id == ip.id
        elif self.id is not None and ip.id is not None:
            matched = False
        elif self.amount is None or self.currency is None:
            matched = False
        else:
            matched = (abs(self.date - ip.date) <= window
                       and self.amount == ip.amount
                       and self.currency == ip.currency)

        if matched:
            self._match = ip
            ip._match = self
        return matched
