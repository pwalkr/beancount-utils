import hashlib
import re
from collections import defaultdict

from beangulp.extract import mark_duplicate_entries


class PostingDeduplicator:
    """Handles posting-level deduplication for beancount entries using hash-based indexing.

    This class creates stable hashes from posting date, description, and amount, then uses
    them to detect duplicate entries. Multiple postings with the same date+description+amount
    are tracked separately with unique identifiers.
    """

    def __init__(self, account, prefix=None, logger=None, meta_key='import_id'):
        """Initialize the deduplicator for a specific account.

        Args:
            account: The account to deduplicate for (e.g., 'Assets:Checking')
            prefix: Optional prefix for generated import_ids
            logger: Optional logger for warnings
            meta_key: The meta key name for import_id (default 'import_id')
        """
        self.account = account
        self.prefix = prefix
        self.logger = logger
        self.meta_key = meta_key
        self.hash_counter = defaultdict(int)  # hash_base -> count of occurrences
        self.imported_hashes = defaultdict(list)  # hash_base -> list of (entry, posting)
        self.imported_ids = set()  # set of import_ids from incoming entries

    def _normalize_description(self, description):
        """Normalize description to reduce formatting differences.

        Normalizes by:
        - Converting to lowercase
        - Removing special characters (keeping alphanumeric, spaces, hyphens)
        - Squashing multiple spaces into single space
        - Stripping leading/trailing whitespace

        Args:
            description: The description/narration to normalize

        Returns:
            Normalized description string
        """
        # Convert to lowercase
        normalized = description.lower()
        # Remove special characters, keep alphanumeric, spaces, hyphens
        normalized = re.sub(r'[^a-z0-9\s\-]', '', normalized)
        # Squash multiple spaces into single space
        normalized = re.sub(r'\s+', ' ', normalized)
        # Strip leading/trailing whitespace
        normalized = normalized.strip()
        return normalized

    def mark_posting(self, date, description, posting):
        """Mark a transaction posting with an import id based on its hash.

        Args:
            date: Transaction date
            description: Transaction description/narration
            posting: The posting within the transaction to generate hash for
        """
        normalized = self._normalize_description(description)

        # Save raw id and count occurrences to handle identical same-day transactions
        raw_id = f"{date}|{normalized}|{posting.units.number}|{posting.units.currency}"
        self.hash_counter[raw_id] += 1
        raw_indexed = f"{raw_id}|{self.hash_counter[raw_id]}"

        hash_val = hashlib.sha256(raw_indexed.encode()).hexdigest()

        import_id = f"{self.prefix}-{hash_val}" if self.prefix else hash_val

        self.imported_ids.add(import_id)

        posting.meta[self.meta_key] = import_id

    def comparator(self):
        """Returns a beangulp.extract.mark_duplicate_entries compatible comparison method."""
        def cmp(entry1, entry2):
            if hasattr(entry1, 'postings') and hasattr(entry2, 'postings'):
                for p1 in entry1.postings:
                    if p1.account == self.account and self.meta_key in p1.meta:
                        for p2 in entry2.postings:
                            if p2.account == self.account:
                                if p2.meta and self.meta_key in p2.meta:
                                    if p1.meta[self.meta_key] == p2.meta[self.meta_key]:
                                        if p1.units.currency == p2.units.currency and abs(p1.units.number - p2.units.number) < 0.00001:
                                            return True
                                        elif logger:
                                            logger.warning(f"Sanity check failed: amounts differ for import_id {p1.meta[meta_key]} ({p1.units.number} vs {p2.units.number})")
            return False
        return cmp

    def deduplicate(self, entries, existing, window=None):
        """Deduplicate entries using import ids.

        """
        window = window or datetime.timedelta(days=2)
        mark_duplicate_entries(entries, existing, window, self.comparator())
