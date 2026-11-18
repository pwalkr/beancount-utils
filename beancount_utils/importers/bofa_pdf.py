"""
Extract beancount entries from a Bank of America deposit account
(checking / savings) PDF statement.

    Importer(
        account="Assets:US:BofA:Checking",
        account_number="1234",         # full number or trailing digits
        decorator=Decorator(...),      # optional categorizer
    )

Text is pulled page by page with pdfplumber's ``extract_text``.  The
importer emits:
  - One transaction per row of the "Deposits and other additions",
    "ATM and debit card subtractions", "Other subtractions", "Checks" and
    "Service fees" tables, with a single posting to ``account``.  The
    amount sign is taken from the statement (subtractions are negative).
  - A balance assertion for the ending balance, dated the day after the
    statement period (assertions apply at the start of the day).

The parsed rows are reconciled against the statement's beginning and
ending balances; a mismatch means a row was missed or misread, so
extraction fails rather than importing a partial statement.

Deduplication also returns ledger transactions on ``account`` dated within
the statement period that match no statement row, tagged ``OUT_OF_PLACE``
with the posting flagged ``!``.  These explain a ledger whose balance
assertion fails because of extra or mistyped entries.

ACH descriptions look like
``ORIGINATOR DES:PAYROLL ID:123 INDN:NAME CO ID:456 PPD``; the
originator becomes the payee.  Other rows use the description with any
trailing confirmation number dropped.  The full description is kept as
the narration.
"""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

import pdfplumber

from beancount.core.data import (
    Amount, Balance, Posting, Transaction, new_metadata,
)
import beangulp
from beangulp import mimetypes

from beancount_utils.deduplicate import (
    extract_out_of_place, mark_duplicate_balances, mark_duplicate_entries,
)
from beancount_utils.decorator import Decorator


DATE = r"[A-Z][a-z]+ \d{1,2}, \d{4}"
MONEY = r"-?\$?-?[\d,]+\.\d{2}"

PERIOD_RE = re.compile(rf"for ({DATE}) to ({DATE})")
ACCOUNT_NUMBER_RE = re.compile(r"Account number:\s*(\d[\d ]*\d)")
BEGINNING_RE = re.compile(rf"^Beginning balance on {DATE}\s+({MONEY})$")
ENDING_RE = re.compile(rf"^Ending balance on {DATE}\s+({MONEY})$")

# Column headers that open a transaction table.  The checks table can be
# printed in two side-by-side column groups on one line.
TABLE_HEADER_RE = re.compile(r"^Date\s+Description\s+Amount$")
CHECKS_HEADER_RE = re.compile(r"^Date\s+Check\s+#\s+Amount\b")

# "MM/DD/YY <description> <amount>"
ROW_RE = re.compile(rf"^(\d{{2}}/\d{{2}}/\d{{2}})\s+(.+?)\s+({MONEY})$")
# "MM/DD/YY <check number>[*] <amount>", possibly twice per line.  An
# asterisk marks a gap in the check sequence.
CHECK_RE = re.compile(rf"(\d{{2}}/\d{{2}}/\d{{2}})\s+(\d+)\*?\s+({MONEY})")

# Lines that close the current table: totals, the page-break marker and
# the page footer.  Anything after them is marketing or legal text.
TABLE_END_RE = re.compile(
    r"^(?:Total\b|continued on the next page|Page \d+ of \d+$)", re.IGNORECASE
)
# Lines inside a table that are neither rows nor descriptions, e.g. the
# per-card grouping of debit card activity.
TABLE_SKIP_RE = re.compile(r"^(?:Subtotal\b|Card account #)", re.IGNORECASE)

ACH_PAYEE_RE = re.compile(r"^(.+?)\s+DES:")
REFERENCE_RE = re.compile(r"\s+(?:Conf|Confirmation)#\s*\S+.*$")


def to_decimal(s: str) -> Decimal:
    """Parse "$1,234.56", "-$1,234.56" or "-1,234.56"."""
    return Decimal(s.replace("$", "").replace(",", "").strip())


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%B %d, %Y").date()


def pdf_to_pages(filepath: str) -> list[str]:
    with pdfplumber.open(filepath) as pdf:
        return [p.extract_text() or "" for p in pdf.pages]


def pdf_first_page(filepath: str) -> str:
    """Cheap text extraction for identify(); avoids reading the whole PDF."""
    with pdfplumber.open(filepath) as pdf:
        if not pdf.pages:
            return ""
        return pdf.pages[0].extract_text() or ""


def parse_period(text: str) -> tuple[date, date]:
    m = PERIOD_RE.search(text)
    if not m:
        raise ValueError("Could not find statement period in PDF")
    return parse_date(m.group(1)), parse_date(m.group(2))


def parse_account_number(text: str) -> str | None:
    m = ACCOUNT_NUMBER_RE.search(text)
    return m.group(1).replace(" ", "") if m else None


def parse_summary(lines: list[str]) -> dict:
    """Pull the beginning and ending balances from the account summary."""
    out: dict = {}
    for raw in lines:
        s = raw.strip()
        for key, regex in (("beginning", BEGINNING_RE), ("ending", ENDING_RE)):
            m = regex.match(s)
            if m and key not in out:
                out[key] = to_decimal(m.group(1))
    return out


def parse_transactions(lines: list[str]) -> list[dict]:
    """Parse every transaction table into rows.

    Returns [{date, description, amount}, ...] in statement order.
    Descriptions that wrap onto following lines are joined back up.
    """
    rows: list[dict] = []
    table = None  # 'rows' | 'checks' | None
    current = None  # last row, for description wraps

    for raw in lines:
        s = raw.strip()
        if not s:
            continue

        if TABLE_HEADER_RE.match(s):
            table, current = "rows", None
            continue
        if CHECKS_HEADER_RE.match(s):
            table, current = "checks", None
            continue
        if table is None:
            continue
        if TABLE_END_RE.match(s):
            table = current = None
            continue
        if TABLE_SKIP_RE.match(s):
            current = None
            continue

        if table == "checks":
            for date_s, number, amount in CHECK_RE.findall(s):
                rows.append({
                    "date": datetime.strptime(date_s, "%m/%d/%y").date(),
                    "description": f"Check {number}",
                    "amount": to_decimal(amount),
                })
            continue

        m = ROW_RE.match(s)
        if m:
            date_s, description, amount = m.groups()
            current = {
                "date": datetime.strptime(date_s, "%m/%d/%y").date(),
                "description": description,
                "amount": to_decimal(amount),
            }
            rows.append(current)
        elif current is not None:
            current["description"] = f"{current['description']} {s}"

    for row in rows:
        row["description"] = re.sub(r"\s+", " ", row["description"]).strip()
    return rows


def payee_of(description: str) -> str:
    m = ACH_PAYEE_RE.match(description)
    if m:
        return m.group(1)
    return REFERENCE_RE.sub("", description)


class Importer(beangulp.Importer):
    """An importer for Bank of America deposit account PDF statements."""

    def __init__(
        self,
        account: str,
        account_number: str,
        currency: str = "USD",
        decorator: Decorator | None = None,
    ):
        self._account = account
        self.account_number = account_number.replace(" ", "")
        self.currency = currency
        self.decorator = decorator
        self._periods: dict[str, tuple[date, date]] = {}

    def identify(self, filepath):
        mimetype, _ = mimetypes.guess_type(filepath)
        if mimetype != "application/pdf":
            return False
        text = pdf_first_page(filepath)
        if "Bank of America" not in text:
            return False
        number = parse_account_number(text)
        return number is not None and number.endswith(self.account_number)

    def account(self, filepath):
        return self._account

    def date(self, filepath):
        _, end = parse_period(pdf_first_page(filepath))
        return end

    def extract(self, filepath, existing):
        pages = pdf_to_pages(filepath)
        lines = "\n".join(pages).splitlines()
        start, end = parse_period(pages[0])
        self._periods[filepath] = (start, end)
        summary = parse_summary(lines)
        rows = parse_transactions(lines)

        if "beginning" in summary and "ending" in summary:
            net = sum((r["amount"] for r in rows), Decimal(0))
            expected = summary["ending"] - summary["beginning"]
            if net != expected:
                raise ValueError(
                    f"Parsed transactions total {net} but the statement "
                    f"balances changed by {expected} in {filepath}"
                )

        # Tables are grouped by type; interleave them chronologically.
        rows.sort(key=lambda r: r["date"])
        entries = [self._transaction(row, filepath, i) for i, row in enumerate(rows)]
        if "ending" in summary:
            entries.append(Balance(
                meta=new_metadata(filepath, len(rows)),
                date=end + timedelta(days=1),
                account=self._account,
                amount=Amount(summary["ending"], self.currency),
                tolerance=None,
                diff_amount=None,
            ))
        return entries

    def _transaction(self, row, filepath, lineno):
        return Transaction(
            meta=new_metadata(filepath, lineno),
            date=row["date"],
            flag="*",
            payee=payee_of(row["description"]),
            narration=row["description"],
            tags=frozenset(),
            links=frozenset(),
            postings=[
                Posting(
                    self._account,
                    Amount(row["amount"], self.currency),
                    None, None, None, None,
                ),
            ],
        )

    def deduplicate(self, entries, existing):
        mark_duplicate_entries(entries, existing, self._account)
        mark_duplicate_balances(entries, existing)
        # Decorate after marking duplicates so the added postings don't
        # interfere with matching.
        if self.decorator:
            self.decorator.decorate(entries, existing)
        # Ledger entries come back already categorized; add them after
        # decorating so rules don't append postings to them.
        start, end = self._period_of(entries)
        if start is not None:
            entries.extend(extract_out_of_place(
                existing, entries, self._account, start=start, end=end,
            ))

    def _period_of(self, entries):
        """Statement period of the file these entries were extracted from."""
        for entry in entries:
            period = self._periods.get(entry.meta.get("filename"))
            if period:
                return period
        return None, None
