"""
Extract beancount entries from a Chase credit card PDF statement.

    Importer(
        account="Liabilities:US:Chase:Amazon",
        account_number="1234",         # full number or trailing digits
        decorator=Decorator(...),      # optional categorizer
    )

Text is pulled page by page with pdfplumber's ``extract_text``.  The
importer emits:
  - One transaction per row of the "ACCOUNT ACTIVITY" table (payments,
    credits, purchases, fees and interest), with a single posting to
    ``account``.  The statement shows charges as positive and credits as
    negative; postings are negated so charges reduce the liability.
  - A balance assertion for the new balance, dated the closing date.
    Assertions apply at the start of the day, so this excludes charges
    dated on the closing date, which Chase may post to the next statement.

Rows carry only MM/DD; the year is taken from the statement period.  The
parsed rows are reconciled against the statement's previous and new
balances; a mismatch means a row was missed or misread, so extraction
fails rather than importing a partial statement.

Deduplication also returns ledger transactions on ``account`` dated within
the statement period that match no statement row, tagged ``OUT_OF_PLACE``
with the posting flagged ``!``.  These explain a ledger whose balance
assertion fails because of extra or mistyped entries.

The merchant descriptor becomes the payee.  Lines that wrap under a row,
such as Amazon's "Order Number ...", are appended to the narration.  The
narration is also kept as ``description`` metadata on the imported
posting, so it survives a decorator rewriting the narration.
"""
import re
from datetime import date, datetime
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


MONEY = r"-?\$?-?[\d,]+\.\d{2}"

PERIOD_RE = re.compile(
    r"Opening/Closing Date\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})"
)
# Older statements print the full number; newer ones mask all but the
# last four digits with X.
ACCOUNT_NUMBER_RE = re.compile(r"Account Number:\s*([X\d][X\d ]*\d)")
PREVIOUS_RE = re.compile(rf"^Previous Balance\s+({MONEY})$")
NEW_RE = re.compile(rf"^New Balance\s+({MONEY})$")

# Column header that opens the activity table, on each page it spans.
TABLE_HEADER_RE = re.compile(r"Merchant Name or Transaction Description")

# "MM/DD <description> <amount>"
ROW_RE = re.compile(rf"^(\d{{2}}/\d{{2}})\s+(.+?)\s+({MONEY})$")

# Lines that close the table: the year-to-date totals that follow it and
# the page footer.  Anything after them is legal or interest-rate text.
TABLE_END_RE = re.compile(r"Totals Year-to-Date|\bPage\s*\d+ of \d+\b", re.IGNORECASE)
# Lines inside the table that are neither rows nor descriptions: the
# section headings and their totals.
TABLE_SKIP_RE = re.compile(
    r"^(?:PAYMENTS AND OTHER CREDITS|PURCHASES?|CASH ADVANCES?"
    r"|BALANCE TRANSFERS?|FEES CHARGED|INTEREST CHARGED)$"
    r"|^TOTAL (?:FEES|INTEREST)\b",
)


def to_decimal(s: str) -> Decimal:
    """Parse "$1,234.56", "-$1,234.56" or "-1,234.56"."""
    return Decimal(s.replace("$", "").replace(",", "").strip())


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%m/%d/%y").date()


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
    """Visible digits of the account number, e.g. "1234" when masked."""
    m = ACCOUNT_NUMBER_RE.search(text)
    if not m:
        return None
    return re.sub(r"[X ]", "", m.group(1)) or None


def parse_summary(lines: list[str]) -> dict:
    """Pull the previous and new balances from the account summary."""
    out: dict = {}
    for raw in lines:
        s = raw.strip()
        for key, regex in (("previous", PREVIOUS_RE), ("new", NEW_RE)):
            m = regex.match(s)
            if m and key not in out:
                out[key] = to_decimal(m.group(1))
    return out


def row_date(mmdd: str, end: date) -> date:
    """Date of an MM/DD row in a statement period ending on ``end``.

    The latest such date not after the closing date, so December rows on
    a statement closing in January land in the previous year.
    """
    month, day = (int(x) for x in mmdd.split("/"))
    for year in (end.year, end.year - 1):
        try:
            d = date(year, month, day)
        except ValueError:  # Feb 29 outside a leap year
            continue
        if d <= end:
            return d
    raise ValueError(f"Could not place {mmdd} in a period ending {end}")


def parse_transactions(lines: list[str], end: date) -> list[dict]:
    """Parse the account activity table into rows.

    Returns [{date, description, amount}, ...] in statement order, with
    amounts signed as printed (charges positive).  Descriptions that wrap
    onto following lines are joined back up.
    """
    rows: list[dict] = []
    in_table = False
    current = None  # last row, for description wraps

    for raw in lines:
        s = raw.strip()
        if not s:
            continue

        if TABLE_HEADER_RE.search(s):
            in_table, current = True, None
            continue
        if not in_table:
            continue
        if TABLE_END_RE.search(s):
            in_table = False
            current = None
            continue
        if TABLE_SKIP_RE.match(s):
            current = None
            continue

        m = ROW_RE.match(s)
        if m:
            mmdd, description, amount = m.groups()
            current = {
                "date": row_date(mmdd, end),
                "merchant": description,
                "description": description,
                "amount": to_decimal(amount),
            }
            rows.append(current)
        elif current is not None:
            current["description"] = f"{current['description']} {s}"

    for row in rows:
        for key in ("merchant", "description"):
            row[key] = re.sub(r"\s+", " ", row[key]).strip()
    return rows


class Importer(beangulp.Importer):
    """An importer for Chase credit card PDF statements."""

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
        if "chase.com" not in text.lower():
            return False
        number = parse_account_number(text)
        # Either side may be just the trailing digits.
        return number is not None and (
            number.endswith(self.account_number)
            or self.account_number.endswith(number)
        )

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
        rows = parse_transactions(lines, end)

        if "previous" in summary and "new" in summary:
            net = sum((r["amount"] for r in rows), Decimal(0))
            expected = summary["new"] - summary["previous"]
            if net != expected:
                raise ValueError(
                    f"Parsed transactions total {net} but the statement "
                    f"balance changed by {expected} in {filepath}"
                )

        # Payments and purchases are grouped; interleave them chronologically.
        rows.sort(key=lambda r: r["date"])
        entries = [self._transaction(row, filepath, i) for i, row in enumerate(rows)]
        if "new" in summary:
            entries.append(Balance(
                meta=new_metadata(filepath, len(rows)),
                date=end,
                account=self._account,
                amount=Amount(-summary["new"], self.currency),
                tolerance=None,
                diff_amount=None,
            ))
        return entries

    def _transaction(self, row, filepath, lineno):
        return Transaction(
            meta=new_metadata(filepath, lineno),
            date=row["date"],
            flag="*",
            payee=row["merchant"],
            narration=row["description"],
            tags=frozenset(),
            links=frozenset(),
            postings=[
                Posting(
                    self._account,
                    Amount(-row["amount"], self.currency),
                    None, None, None,
                    {"description": row["description"]},
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
