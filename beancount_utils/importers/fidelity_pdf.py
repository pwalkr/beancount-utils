"""
Extract beancount entries from a Fidelity brokerage statement PDF.

Emits:
  - one Buy transaction per row in "Securities Bought & Sold" (You Bought)
  - one Sell transaction per row in "Securities Bought & Sold" (You Sold)
  - one Dividend transaction per row in "Dividends, Interest & Other Income"
  - one balance assertion per held commodity at period-end + 1 day
    (cash held in the core/sweep account asserts in `currency`)

Account layout:
  Assets:               {account}:{COMMODITY}      (cash uses :USD)
  Income (dividends):   {income_base}:{COMMODITY}

To dump pages for importer development, use:
  python -c "from beancount_utils.importers.fidelity_pdf import pdf_to_pages; \
    print('\n\n===PAGE BREAK===\n\n'.join(pdf_to_pages('path/to/statement.pdf')))" > /tmp/fidelity_dump.txt
"""
import re
from datetime import date, timedelta
from decimal import Decimal

import pdfplumber

from beancount.core.data import Amount, Balance, Posting, Transaction, new_metadata
from beancount.core.position import CostSpec
import beangulp
from beangulp import mimetypes


PERIOD_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d+,\s+(\d{4})\s*-\s*"
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d+),\s+(\d{4})"
)
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1)}

TICKER_RE = re.compile(r"\(([A-Z]{2,6})\)")

# Holdings row data: begin_mv, quantity, price, ending_mv
HOLDING_DATA_RE = re.compile(
    r"\$?([\d,]+\.\d{2,4})\s+"
    r"([\d,]+\.\d{1,3})\s+"
    r"\$?([\d,]+\.\d{2,4})\s+"
    r"\$?([\d,]+\.\d{2,4})\s+"
)

# Dividend row: "MM/DD <name> <cusip> Dividend Received - - $amount"
DIVIDEND_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)\s+([\dA-Z]{9})\s+"
    r"Dividend Received\s+-\s+-\s+\$?([\d,]+\.\d{2})$"
)

# Buy/Sell row: "MM/DD <name> <cusip> You (Bought|Sold) qty price cost amount"
# Cost column is "-" when not reported (stock buys); amount may be negative for buys.
TRADE_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)\s+([\dA-Z]{9})\s+"
    r"You\s+(Bought|Sold)\s+"
    r"([\d,]+\.?\d*)\s+\$?([\d,]+\.\d{2,6})\s+"
    r"(?:-|\$?-?[\d,]+\.\d{2,6})\s+"
    r"-?\$?([\d,]+\.\d{2})$"
)

# Deposit / Withdrawal row: "MM/DD <description> [<reference>] $amount"
TRANSFER_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)\s+-?\$?(-?[\d,]+\.\d{2})$"
)


def to_decimal(s: str) -> Decimal:
    return Decimal(s.replace("$", "").replace(",", "").strip())


def parse_period(text: str) -> tuple[date, date]:
    m = PERIOD_RE.search(text)
    if not m:
        raise ValueError("Could not find statement period in PDF")
    start = date(int(m.group(2)), MONTHS[m.group(1)], 1)
    end = date(int(m.group(5)), MONTHS[m.group(3)], int(m.group(4)))
    return start, end


def pdf_to_pages(filepath: str) -> list[str]:
    with pdfplumber.open(filepath) as pdf:
        return [p.extract_text() or "" for p in pdf.pages]


def pdf_first_page(filepath: str) -> str:
    """Cheap text extraction for identify(); avoids reading the whole PDF."""
    with pdfplumber.open(filepath) as pdf:
        if not pdf.pages:
            return ""
        return pdf.pages[0].extract_text() or ""


def resolve_year(month: int, start: date, end: date) -> int:
    if start.year == end.year:
        return start.year
    return start.year if month >= start.month else end.year


def parse_holdings(pages: list[str]) -> dict[str, dict]:
    """Return {ticker: {name, quantity, price, ending_value, is_core}}."""
    holdings: dict[str, dict] = {}
    in_holdings = False
    section = None

    for text in pages:
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line == "Holdings":
                in_holdings = True
                i += 1
                continue
            if in_holdings and (
                line.startswith("Activity")
                or line.startswith("Estimated Cash Flow")
                or line.startswith("Total Holdings")
                or line.startswith("Additional Information")
            ):
                in_holdings = False
            if not in_holdings:
                i += 1
                continue

            if line.startswith("Core Account") and "Total" not in line:
                section = "core"
            elif line.startswith("Exchange Traded Products") and "Total" not in line:
                section = "etp"
            elif line.startswith(("Stocks", "Mutual Funds", "Bonds", "Options", "Other")) and "Total" not in line:
                section = "other"

            if line.startswith("Total"):
                i += 1
                continue

            m = HOLDING_DATA_RE.search(line)
            if not m:
                i += 1
                continue

            name = line[: m.start()].strip()
            if not name or name.startswith("$"):
                i += 1
                continue

            ticker = None
            tm = TICKER_RE.search(line)
            if tm:
                ticker = tm.group(1)
            elif i + 1 < len(lines):
                tm2 = TICKER_RE.search(lines[i + 1])
                if tm2:
                    ticker = tm2.group(1)
                    extra = lines[i + 1].strip().split("(")[0].strip()
                    if extra:
                        name = f"{name} {extra}".strip()

            if ticker:
                begin_mv, qty, price, ending_mv = m.groups()
                holdings[ticker] = {
                    "name": name,
                    "quantity": to_decimal(qty),
                    "price": to_decimal(price),
                    "ending_value": to_decimal(ending_mv),
                    "is_core": section == "core",
                }
            i += 1

    return holdings


def parse_dividends(pages: list[str], start: date, end: date) -> list[dict]:
    out: list[dict] = []
    in_section = False
    pending_name_tail = None

    for text in pages:
        for line in text.splitlines():
            stripped = line.strip()
            if "Dividends, Interest & Other Income" in stripped and not stripped.startswith("Total"):
                in_section = True
                continue
            if in_section and (
                stripped.startswith("Total Dividends")
                or stripped.startswith("Deposits")
                or stripped.startswith("Core Fund Activity")
                or stripped.startswith("Securities Bought")
            ):
                in_section = False
            if not in_section:
                continue

            m = DIVIDEND_RE.match(stripped)
            if m:
                mm_dd, name, _cusip, amount = m.groups()
                month, day = (int(x) for x in mm_dd.split("/"))
                year = resolve_year(month, start, end)
                out.append({
                    "date": date(year, month, day),
                    "name": name.strip(),
                    "amount": to_decimal(amount),
                })
                pending_name_tail = out[-1]
                continue

            # Wrapped name continuation (e.g. "MARKET" on its own line)
            if (
                pending_name_tail
                and stripped
                and not stripped[0].isdigit()
                and not stripped.startswith("Total")
                and stripped == stripped.upper()
                and len(stripped.split()) <= 3
            ):
                pending_name_tail["name"] = f"{pending_name_tail['name']} {stripped}".strip()
            pending_name_tail = None

    return out


def parse_trades(pages: list[str], start: date, end: date) -> list[dict]:
    out: list[dict] = []
    in_section = False
    for text in pages:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("Securities Bought") or stripped.startswith(
                "Trades Pending Settlement"
            ):
                in_section = True
                continue
            if in_section and (
                stripped.startswith("Total Securities")
                or stripped.startswith("Dividends, Interest")
                or stripped.startswith("Core Fund Activity")
                or stripped.startswith("Deposits")
            ):
                in_section = False
            if not in_section:
                continue
            m = TRADE_RE.match(stripped)
            if not m:
                continue
            mm_dd, name, _cusip, side, qty, price, amount = m.groups()
            month, day = (int(x) for x in mm_dd.split("/"))
            out.append({
                "date": date(resolve_year(month, start, end), month, day),
                "name": name.strip(),
                "side": side,
                "quantity": to_decimal(qty),
                "price": to_decimal(price),
                "amount": to_decimal(amount),
            })
    return out


def parse_transfers(pages: list[str], start: date, end: date) -> list[dict]:
    """Parse the Deposits and Withdrawals sections.

    Returns dicts with {date, description, amount} where amount is signed:
    positive for deposits (cash in), negative for withdrawals (cash out).
    """
    out: list[dict] = []
    section = None  # "deposit" | "withdrawal" | None
    for text in pages:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "Deposits":
                section = "deposit"
                continue
            if stripped == "Withdrawals":
                section = "withdrawal"
                continue
            if section and (
                stripped.startswith("Total Deposits")
                or stripped.startswith("Total Withdrawals")
                or stripped.startswith("Core Fund Activity")
                or stripped.startswith("Dividends, Interest")
                or stripped.startswith("Securities Bought")
            ):
                section = None
            if not section:
                continue
            if stripped.startswith("Date "):  # column header
                continue
            m = TRANSFER_RE.match(stripped)
            if not m:
                continue
            mm_dd, description, amount = m.groups()
            month, day = (int(x) for x in mm_dd.split("/"))
            amt = to_decimal(amount)
            if section == "withdrawal" and amt > 0:
                amt = -amt
            out.append({
                "date": date(resolve_year(month, start, end), month, day),
                "description": description.strip(),
                "amount": amt,
            })
    return out


def resolve_ticker(div_name: str, name_to_ticker: dict[str, str]) -> str | None:
    if div_name in name_to_ticker:
        return name_to_ticker[div_name]
    for n, t in name_to_ticker.items():
        if div_name.startswith(n) or n.startswith(div_name):
            return t
    return None


class Importer(beangulp.Importer):
    """An importer for Fidelity brokerage statement PDFs."""

    def __init__(
        self,
        account: str,
        income_base: str | None = None,
        account_id: str | None = None,
        currency: str = "USD",
    ):
        self._account_base = account
        self.income_base = (
            income_base if income_base is not None else account.replace("Assets", "Income", 1)
        )
        self.account_id = account_id
        self.currency = currency

    @property
    def cash_account(self) -> str:
        return f"{self._account_base}:{self.currency}"

    def sub_account(self, commodity: str) -> str:
        return f"{self._account_base}:{commodity}"

    def income_account(self, commodity: str) -> str:
        return f"{self.income_base}:{commodity}"

    def identify(self, filepath):
        mimetype, _ = mimetypes.guess_type(filepath)
        if mimetype != "application/pdf":
            return False
        text = pdf_first_page(filepath)
        if "Fidelity" not in text and "FIDELITY" not in text:
            return False
        if self.account_id and self.account_id not in text:
            return False
        return PERIOD_RE.search(text) is not None

    def account(self, filepath):
        return self._account_base

    def extract(self, filepath, existing):
        pages = pdf_to_pages(filepath)
        full_text = "\n".join(pages)
        start, end = parse_period(full_text)

        holdings = parse_holdings(pages)
        name_to_ticker = {h["name"]: t for t, h in holdings.items()}

        entries = []

        for div in parse_dividends(pages, start, end):
            ticker = resolve_ticker(div["name"], name_to_ticker)
            if ticker is None:
                raise KeyError(
                    f"Could not resolve ticker for dividend {div['name']!r} in {filepath}"
                )
            entries.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=div["date"],
                flag="*",
                payee=None,
                narration=f"Dividend - {ticker}",
                tags=frozenset(),
                links=frozenset(),
                postings=[
                    Posting(self.income_account(ticker), None, None, None, None, None),
                    Posting(
                        self.cash_account,
                        Amount(div["amount"], self.currency),
                        None, None, None, None,
                    ),
                ],
            ))

        for trade in parse_trades(pages, start, end):
            ticker = resolve_ticker(trade["name"], name_to_ticker)
            if ticker is None:
                raise KeyError(
                    f"Could not resolve ticker for trade {trade['name']!r} in {filepath}"
                )
            is_buy = trade["side"] == "Bought"
            qty = trade["quantity"]
            price = trade["price"]
            amount = trade["amount"]

            if is_buy:
                cash_posting = Posting(
                    self.cash_account,
                    Amount(-amount, self.currency),
                    None, None, None, None,
                )
                sec_posting = Posting(
                    self.sub_account(ticker),
                    Amount(qty, ticker),
                    CostSpec(price, None, self.currency, None, None, None),
                    None, None, None,
                )
                postings = [cash_posting, sec_posting]
            else:
                postings = [
                    Posting(
                        self.cash_account,
                        Amount(amount, self.currency),
                        None, None, None, None,
                    ),
                    Posting(
                        self.sub_account(ticker),
                        Amount(-qty, ticker),
                        CostSpec(None, None, None, None, None, None),
                        Amount(price, self.currency),
                        None, None,
                    ),
                    Posting(self.income_account(ticker), None, None, None, None, None),
                ]

            entries.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=trade["date"],
                flag="*",
                payee=None,
                narration=f"{'Buy' if is_buy else 'Sell'} {ticker}",
                tags=frozenset(),
                links=frozenset(),
                postings=postings,
            ))

        for xfer in parse_transfers(pages, start, end):
            narration = (
                f"Deposit - {xfer['description']}"
                if xfer["amount"] > 0
                else f"Withdrawal - {xfer['description']}"
            )
            entries.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=xfer["date"],
                flag="*",
                payee=None,
                narration=narration,
                tags=frozenset(),
                links=frozenset(),
                postings=[
                    Posting(
                        self.cash_account,
                        Amount(xfer["amount"], self.currency),
                        None, None, None, None,
                    ),
                ],
            ))

        next_day = end + timedelta(days=1)
        for ticker, h in holdings.items():
            if h["is_core"]:
                entries.append(Balance(
                    meta=new_metadata(filepath, 0),
                    date=next_day,
                    account=self.cash_account,
                    amount=Amount(h["ending_value"], self.currency),
                    tolerance=None,
                    diff_amount=None,
                ))
            else:
                entries.append(Balance(
                    meta=new_metadata(filepath, 0),
                    date=next_day,
                    account=self.sub_account(ticker),
                    amount=Amount(h["quantity"], ticker),
                    tolerance=None,
                    diff_amount=None,
                ))

        return entries
