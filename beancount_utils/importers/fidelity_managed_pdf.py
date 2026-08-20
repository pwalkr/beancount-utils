"""
Extract beancount entries from a Fidelity "INVESTMENT REPORT" PDF that
contains one or more accounts (e.g. Fidelity Go managed portfolios,
HSAs, and brokerage IRAs bundled in a single statement).

Each account is configured via an entry passed to the Importer:

    Importer(accounts=[
        {"account_id":      "Z89-123456",
         "account":         "Assets:US:Fidelity:RothIRA",
         "income_base":     "Income:US:Fidelity:RothIRA",       # optional
         "expense_account": "Expenses:Financial:Fees:Fidelity", # optional
         "equity_account":  "Equity:Contributions"},            # optional
        ...
    ])

For each configured account the importer emits:
  - Dividend transactions  (Income -> Cash)
  - Reinvestment buys      (Cash -> Sub-account, at cost)
  - Contributions          (Equity -> Cash)  if equity_account is set
  - Advisory / other fees  (Cash -> Expenses) if expense_account is set
  - Per-holding balance assertions at period-end + 1 day.
    The core/sweep fund (Core Account or Short-term Funds money market)
    is treated as cash and asserted in USD.
  - Per-holding price directives at period-end, from the "Price Per Unit"
    column of the Holdings tables (the cash fund is priced at $1 and is
    skipped).

Sweep activity in the "Core Fund Activity" table is intentionally ignored
because it is just the internal representation of cash flows that are
already booked from the other sections.
"""
import re
from datetime import date, timedelta
from decimal import Decimal

import pdfplumber

from beancount.core.data import (
    Amount, Balance, Posting, Price, Transaction, new_metadata,
)
from beancount.core.position import CostSpec
import beangulp
from beangulp import mimetypes

from beancount_utils.importers.fidelity_pdf import (
    MONTHS,
    PERIOD_RE,
    TICKER_RE,
    parse_period,
    pdf_first_page,
    pdf_to_pages,
    resolve_year,
    resolve_ticker,
    to_decimal,
)


ACCOUNT_HEADER_RE = re.compile(r"Account # ([A-Z0-9]+-[A-Z0-9]+)")

# Holdings rows surround the three columns we care about (quantity, price
# per unit, ending market value) with a varying number of others: percent
# of holdings, beginning market value, cost, gain/loss and EAI.  Match the
# triple directly rather than counting columns from the left, and confirm
# the alignment with quantity * price.
HOLDING_TRIPLE_RE = re.compile(
    r"([\d,]+\.\d{2,3})\s+\$?([\d,]+\.\d{2,5})\s+\$?([\d,]+\.\d{2})(?=\s|$)"
)

# Beginning market value, printed as "unavailable" for a position that was
# opened during the statement period.
BEGIN_VALUE_RE = re.compile(
    r"\s+(?:\$?[\d,]+\.\d{2}|unavailable|not applicable)$", re.IGNORECASE
)
# Percent-of-holdings column.  It always carries a decimal point or a
# percent sign, unlike a number that belongs to the security name
# (e.g. "PROSHARES TRUST COINDESK 20").
PERCENT_COL_RE = re.compile(r"\s+\d{1,3}(?:\.\d+%?|%)$")
# Placeholder dashes for empty trailing columns.
TRAILING_DASH_RE = re.compile(r"(?:\s+-+)+$")

# Security name as printed in the Activity tables.
NAME = r"[A-Z][A-Z0-9&.()'\-/ ]+?"
# A Cost / Cost Basis / Transaction Cost column, "-" when not applicable.
# Only some layouts have them, and a charge is printed as "-$0.02".
COST_COL = r"(?:-|-?\$?-?[\d,]+\.\d+)"

# Dividend row variants - column order differs between managed and
# brokerage account layouts, and rows after the first in a managed
# security block carry no name.
DIVIDEND = r"Dividend Received\s+-\s+-(?:\s+-)?\s+\$?([\d,]+\.\d{2})$"
DIVIDEND_RE_NAME_FIRST = re.compile(
    rf"^({NAME})\s+(\d{{2}}/\d{{2}})\s+([\dA-Z]{{9}})\s+{DIVIDEND}"
)
DIVIDEND_RE_DATE_FIRST = re.compile(
    rf"^(\d{{2}}/\d{{2}})\s+({NAME})\s+([\dA-Z]{{9}})\s+{DIVIDEND}"
)
DIVIDEND_RE_NO_NAME = re.compile(
    rf"^(\d{{2}}/\d{{2}})\s+([\dA-Z]{{9}})\s+{DIVIDEND}"
)

# Reinvestment row (auto-buy of dividend proceeds back into the same fund):
#   "MM/DD CUSIP Reinvestment qty price [cost] -amount"
REINVEST_RE = re.compile(
    r"^(\d{2}/\d{2})\s+([\dA-Z]{9})\s+"
    r"Reinvestment\s+([\d,]+\.\d+)\s+\$?([\d,]+\.\d+)\s+"
    rf"(?:{COST_COL}\s+)?"
    r"-?\$?(-?[\d,]+\.\d{2})$"
)

# Trade rows.  Brokerage layout (Securities Bought & Sold table) puts the
# date first and has two trailing "Cost" columns before the amount:
#   "MM/DD <name> <cusip> You Bought/Sold <qty> <price> <cost> <cost> <amount>"
# Either cost column may be "-" when not applicable.  Quantity is negative
# for sells, amount is negative for buys; both are captured as magnitudes.
TRADE = (
    r"You\s+(Bought|Sold)\s+"
    r"-?([\d,]+\.\d+)\s+\$?([\d,]+\.\d+)"
    rf"(?:\s+{COST_COL}){{0,2}}\s+"
    r"-?\$?([\d,]+\.\d{2})$"
)
TRADE_RE_DATE_FIRST = re.compile(
    rf"^(\d{{2}}/\d{{2}})\s+({NAME})\s+([\dA-Z]{{9}})\s+{TRADE}"
)
# Managed layout (per-security activity rows) puts the name first, and
# only on the first row of each security block:
#   "<name> MM/DD <cusip> You Bought <qty> <price> [<cost>] -<amount>"
TRADE_RE_NAME_FIRST = re.compile(
    rf"^({NAME})\s+(\d{{2}}/\d{{2}})\s+([\dA-Z]{{9}})\s+{TRADE}"
)
TRADE_RE_NO_NAME = re.compile(
    rf"^(\d{{2}}/\d{{2}})\s+([\dA-Z]{{9}})\s+{TRADE}"
)

# Contribution row: "MM/DD <description> <reference> $amount"
CONTRIBUTION_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)\s+-?\$?(-?[\d,]+\.\d{2})$"
)

# Fee row.  The PDF lays this section out as two columns on a single
# line, so we strip any trailing "Total Fees and Charges ..." before
# matching.  Amount is negative for a charge.
FEE_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)\s+-\$?([\d,]+\.\d{2})$"
)

# Tax-withheld row: "MM/DD <security name> <tax description> -$amount".
TAX_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)\s+"
    r"(Foreign Tax Paid|Federal Tax Withheld|State Tax Withheld|"
    r"Backup Withholding|Non-Resident Alien Tax Withheld)\s+"
    r"-?\$?-?([\d,]+\.\d{2})$"
)

CHANGE_IN_VALUE_RE = re.compile(
    r"^Change in Investment Value\s*\*?\s+(-?[\d,]+\.\d{2})\b"
)
ENDING_VALUE_RE = re.compile(
    r"^Ending Account Value\s+\$?(-?[\d,]+\.\d{2})\b"
)

CASH_SECTIONS = {"core", "short_term"}

PAYEE_FIDELITY = "Fidelity"


def _clean_name(text: str) -> str:
    """Strip the numeric columns that trail a description fragment."""
    text = TRAILING_DASH_RE.sub("", text.strip())
    text = BEGIN_VALUE_RE.sub("", text)
    return PERCENT_COL_RE.sub("", text).strip()


def _match_holding_row(line: str):
    """Locate quantity / price / ending market value in a holdings row.

    Several column alignments can match the pattern, so prefer the one
    where quantity * price reproduces the ending market value; fall back
    to the leftmost match when none does.
    """
    first = None
    for m in HOLDING_TRIPLE_RE.finditer(line):
        if first is None:
            first = m
        qty, price, ending = (to_decimal(g) for g in m.groups())
        tolerance = max(Decimal("0.05"), ending * Decimal("0.001"))
        if abs(qty * price - ending) <= tolerance:
            return m
    return first


def parse_summary(lines: list[str]) -> dict:
    """Pull period-level Change in Investment Value + Ending Account Value."""
    out: dict = {}
    for raw in lines:
        s = raw.strip()
        if "change_in_value" not in out:
            m = CHANGE_IN_VALUE_RE.match(s)
            if m:
                out["change_in_value"] = to_decimal(m.group(1))
                continue
        if "ending_value" not in out:
            m = ENDING_VALUE_RE.match(s)
            if m:
                out["ending_value"] = to_decimal(m.group(1))
    return out


def parse_holdings(lines: list[str]) -> dict[str, dict]:
    """Parse Holdings rows for one account.

    Returns {ticker: {name, quantity, price, ending_value, is_cash}}.
    """
    holdings: dict[str, dict] = {}
    section = None

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith("Core Account") and "Total" not in line:
            section = "core"
        elif line.startswith("Stock Funds") and "Total" not in line:
            section = "stock_fund"
        elif line.startswith("Bond Funds") and "Total" not in line:
            section = "bond_fund"
        elif line.startswith("Short-term Funds") and "Total" not in line:
            section = "short_term"
        elif line.startswith("Exchange Traded Products") and "Total" not in line:
            section = "etp"
        elif line.startswith(("Stocks", "Common Stock")) and "Total" not in line:
            section = "stock"

        if not section or line.startswith("Total"):
            i += 1
            continue

        m = _match_holding_row(line)
        if not m:
            i += 1
            continue

        name = _clean_name(line[: m.start()])
        if not name or name.startswith("$"):
            i += 1
            continue

        ticker = None
        tm = TICKER_RE.search(line)
        if tm:
            ticker = tm.group(1)
        else:
            # Ticker can wrap to the next 1-2 lines along with the rest
            # of the description.
            for j in (1, 2, 3):
                if i + j >= len(lines):
                    break
                nxt = lines[i + j].strip()
                tm2 = TICKER_RE.search(nxt)
                if tm2:
                    ticker = tm2.group(1)
                    extra = _clean_name(nxt.split("(")[0])
                    if extra:
                        name = f"{name} {extra}".strip()
                    break
                # Pure description wrap (no ticker yet).
                cleaned = _clean_name(nxt)
                if cleaned and cleaned == cleaned.upper() and not cleaned[0].isdigit():
                    name = f"{name} {cleaned}".strip()

        if ticker:
            qty, price, ending_mv = m.groups()
            holdings[ticker] = {
                "name": name,
                "quantity": to_decimal(qty),
                "price": to_decimal(price),
                "ending_value": to_decimal(ending_mv),
                "is_cash": section in CASH_SECTIONS,
            }
        i += 1

    return holdings


def _match_dividend(stripped: str):
    """Return (name | None, mm_dd, cusip, amount) for a dividend row."""
    m = DIVIDEND_RE_NAME_FIRST.match(stripped)
    if m:
        name, mm_dd, cusip, amount = m.groups()
        return name, mm_dd, cusip, amount
    m = DIVIDEND_RE_DATE_FIRST.match(stripped)
    if m:
        mm_dd, name, cusip, amount = m.groups()
        return name, mm_dd, cusip, amount
    m = DIVIDEND_RE_NO_NAME.match(stripped)
    if m:
        mm_dd, cusip, amount = m.groups()
        return None, mm_dd, cusip, amount
    return None


def _match_trade(stripped: str):
    """Return (name | None, mm_dd, cusip, side, qty, price, amount)."""
    m = TRADE_RE_NAME_FIRST.match(stripped)
    if m:
        name, mm_dd, cusip, side, qty, price, amount = m.groups()
        return name, mm_dd, cusip, side, qty, price, amount
    m = TRADE_RE_NO_NAME.match(stripped)
    if m:
        mm_dd, cusip, side, qty, price, amount = m.groups()
        return None, mm_dd, cusip, side, qty, price, amount
    return None


def parse_activity(
    lines: list[str], start: date, end: date
) -> dict[str, list[dict]]:
    """Parse one account's activity section into typed rows.

    Returns {'dividends': [...], 'reinvestments': [...],
             'contributions': [...], 'fees': [...]}.
    Reinvestments carry their paired dividend CUSIP so the caller can
    look up the ticker.
    """
    out = {
        "dividends": [],
        "reinvestments": [],
        "contributions": [],
        "fees": [],
        "trades": [],
        "taxes": [],
    }
    section = None  # 'div' | 'contrib' | 'fees' | 'trades' | 'taxes' | 'skip' | None
    pending_div = None  # last row dict, for name-wrap continuation
    # In the managed layout only the first row of a security block names
    # it; the rows below carry just a date and a CUSIP.
    pending_security = None

    def security_name() -> str:
        return pending_security["name"] if pending_security else ""

    def to_d(month, day):
        m, d = int(month), int(day)
        return date(resolve_year(m, start, end), m, d)

    for raw in lines:
        s = raw.strip()
        if not s:
            continue

        # Section transitions
        if "Core Fund Activity" in s:
            section = "skip"
            pending_div = pending_security = None
            continue
        if s.startswith("Realized Gains") or s.startswith("Estimated Cash Flow"):
            section = "skip"
            pending_div = pending_security = None
            continue
        if s.startswith("Additional Information") or ACCOUNT_HEADER_RE.search(s):
            section = None
            pending_div = pending_security = None
            continue
        if "Dividends, Interest" in s and not s.startswith("Total"):
            section = "div"
            pending_div = pending_security = None
            continue
        if s.startswith("Securities Bought"):
            section = "trades"
            pending_div = pending_security = None
            continue
        if s.startswith("Net Securities") or s.startswith("Transaction Profit") \
                or s.startswith("Transaction Loss"):
            # Sub-totals and per-trade P&L annotations - ignore.
            continue
        if s == "Activity":
            # Default to dividend section if no explicit header follows.
            section = "div"
            pending_div = pending_security = None
            continue
        if s == "Contributions" or s == "Distributions":
            section = "contrib"
            pending_div = pending_security = None
            continue
        if s.startswith("Fees and Charges"):
            section = "fees"
            pending_div = pending_security = None
            continue
        if s.startswith("Taxes Withheld"):
            section = "taxes"
            pending_div = pending_security = None
            continue
        if s.startswith("Total "):
            pending_div = pending_security = None
            # Don't change section; another subsection header may follow.
            continue

        if section == "skip" or section is None:
            continue

        if section == "div":
            div = _match_dividend(s)
            if div:
                name, mm_dd, cusip, amount = div
                month, day = mm_dd.split("/")
                entry = {
                    "date": to_d(month, day),
                    "name": name.strip() if name else security_name(),
                    "cusip": cusip,
                    "amount": to_decimal(amount),
                }
                out["dividends"].append(entry)
                if name:
                    pending_div = pending_security = entry
                else:
                    pending_div = None
                continue

            tm = _match_trade(s)
            if tm:
                name, mm_dd, cusip, side, qty, price, amount = tm
                month, day = mm_dd.split("/")
                entry = {
                    "date": to_d(month, day),
                    "name": name.strip() if name else security_name(),
                    "cusip": cusip,
                    "side": side,
                    "quantity": to_decimal(qty),
                    "price": to_decimal(price),
                    "amount": to_decimal(amount),
                }
                out["trades"].append(entry)
                if name:
                    pending_div = pending_security = entry
                else:
                    pending_div = None
                continue

            rm = REINVEST_RE.match(s)
            if rm:
                mm_dd, cusip, qty, price, amount = rm.groups()
                month, day = mm_dd.split("/")
                out["reinvestments"].append({
                    "date": to_d(month, day),
                    "cusip": cusip,
                    "quantity": to_decimal(qty),
                    "price": to_decimal(price),
                    "amount": to_decimal(amount),
                })
                pending_div = None
                continue

            # Name-wrap continuation for the previous row (e.g.
            # "FUND" or "MARKET" on its own line, possibly across
            # multiple lines).
            if (
                pending_div
                and s == s.upper()
                and not s[0].isdigit()
                and len(s.split()) <= 4
            ):
                pending_div["name"] = f"{pending_div['name']} {s}".strip()
                continue
            pending_div = pending_security = None
            continue

        if section == "contrib":
            if s.startswith("Date ") or s.startswith("Reference"):
                continue
            cm = CONTRIBUTION_RE.match(s)
            if cm:
                mm_dd, desc, amount = cm.groups()
                month, day = mm_dd.split("/")
                out["contributions"].append({
                    "date": to_d(month, day),
                    "description": desc.strip(),
                    "amount": to_decimal(amount),
                })
            continue

        if section == "trades":
            if s.startswith("Date ") or s.startswith("Settlement") \
                    or s.startswith("Security Name"):
                continue
            tm = TRADE_RE_DATE_FIRST.match(s)
            if tm:
                mm_dd, name, cusip, side, qty, price, amount = tm.groups()
                month, day = mm_dd.split("/")
                out["trades"].append({
                    "date": to_d(month, day),
                    "name": name.strip(),
                    "cusip": cusip,
                    "side": side,
                    "quantity": to_decimal(qty),
                    "price": to_decimal(price),
                    "amount": to_decimal(amount),
                })
            continue

        if section == "taxes":
            if s.startswith("Date "):
                continue
            tm = TAX_RE.match(s)
            if tm:
                mm_dd, name, kind, amount = tm.groups()
                month, day = mm_dd.split("/")
                out["taxes"].append({
                    "date": to_d(month, day),
                    "name": name.strip(),
                    "kind": kind,
                    "amount": to_decimal(amount),
                })
            continue

        if section == "fees":
            if s.startswith("Date "):
                continue
            # Strip trailing "Total Fees and Charges ..." second column.
            trimmed = re.sub(r"\s+Total\s+Fees\s+and\s+Charges.*$", "", s)
            fm = FEE_RE.match(trimmed)
            if fm:
                mm_dd, desc, amount = fm.groups()
                month, day = mm_dd.split("/")
                out["fees"].append({
                    "date": to_d(month, day),
                    "description": desc.strip(),
                    "amount": to_decimal(amount),
                })
            continue

    return out


def parse_pdf(pages: list[str]) -> dict[str, dict]:
    """Split the full PDF into per-account holdings + activity buckets."""
    per_account: dict[str, dict] = {}
    current = None  # account_id

    # Step 1: route every line to its owning account.
    account_lines: dict[str, list[str]] = {}
    for text in pages:
        for line in text.splitlines():
            m = ACCOUNT_HEADER_RE.search(line)
            if m:
                current = m.group(1)
                account_lines.setdefault(current, [])
                continue
            if current is None:
                continue
            account_lines[current].append(line)

    # Step 2: parse holdings and activity per account.
    for acct, lines in account_lines.items():
        # Split into Holdings region vs Activity region.  Each account
        # has both, but they may appear interleaved across pages.  The
        # holdings parser is tolerant of activity lines (no HOLDING
        # match) and vice-versa, so we just feed the whole stream to
        # each parser.
        holdings = parse_holdings(lines)
        per_account[acct] = {"holdings": holdings}

    return per_account, account_lines


class Importer(beangulp.Importer):
    """An importer for Fidelity multi-account INVESTMENT REPORT PDFs."""

    def __init__(
        self,
        accounts: list[dict],
        currency: str = "USD",
        expense_account: str | None = None,
    ):
        if not accounts:
            raise ValueError("accounts must contain at least one entry")
        self._accounts: dict[str, dict] = {}
        for entry in accounts:
            acct_id = entry["account_id"]
            base = entry["account"]
            self._accounts[acct_id] = {
                "account": base,
                "income_base": entry.get(
                    "income_base", base.replace("Assets", "Income", 1)
                ),
                "expense_account": entry.get("expense_account", expense_account),
                "equity_account": entry.get("equity_account"),
                "summary": entry.get("summary", False),
            }
        self.currency = currency

    def cash_account(self, acct_id: str) -> str:
        return f"{self._accounts[acct_id]['account']}:{self.currency}"

    def sub_account(self, acct_id: str, commodity: str) -> str:
        return f"{self._accounts[acct_id]['account']}:{commodity}"

    def income_account(self, acct_id: str, commodity: str) -> str:
        return f"{self._accounts[acct_id]['income_base']}:{commodity}"

    def identify(self, filepath):
        mimetype, _ = mimetypes.guess_type(filepath)
        if mimetype != "application/pdf":
            return False
        text = pdf_first_page(filepath)
        if "INVESTMENT REPORT" not in text and "Investment Report" not in text:
            return False
        if "Fidelity" not in text and "FIDELITY" not in text:
            return False
        if PERIOD_RE.search(text) is None:
            return False
        with pdfplumber.open(filepath) as pdf:
            if len(pdf.pages) < 2:
                return False
            page2 = pdf.pages[1].extract_text() or ""
        return any(acct_id in page2 for acct_id in self._accounts)

    def account(self, filepath):
        # When multiple accounts share a PDF, return the first
        # configured one as the "owning" account for filing purposes.
        first = next(iter(self._accounts.values()))
        return first["account"]

    def extract(self, filepath, existing):
        pages = pdf_to_pages(filepath)
        full_text = "\n".join(pages)
        start, end = parse_period(full_text)

        _, account_lines = parse_pdf(pages)

        entries = []
        next_day = end + timedelta(days=1)
        priced: set[str] = set()  # one price per commodity per statement

        for acct_id, cfg in self._accounts.items():
            if acct_id not in account_lines:
                continue
            lines = account_lines[acct_id]
            activity = parse_activity(lines, start, end)

            if cfg["summary"]:
                entries.extend(
                    self._emit_summary(lines, activity, end, next_day, acct_id, filepath)
                )
                continue

            holdings = parse_holdings(lines)
            name_to_ticker = {h["name"]: t for t, h in holdings.items()}
            cusip_to_ticker = self._build_cusip_map(activity, name_to_ticker)
            cash_tickers = {t for t, h in holdings.items() if h["is_cash"]}

            entries.extend(
                self._emit_dividends(
                    activity["dividends"], name_to_ticker, cash_tickers,
                    cusip_to_ticker, acct_id, filepath,
                )
            )
            entries.extend(
                self._emit_reinvestments(
                    activity["reinvestments"], cusip_to_ticker, cash_tickers,
                    acct_id, filepath,
                )
            )
            entries.extend(
                self._emit_trades(
                    activity["trades"], name_to_ticker, cash_tickers,
                    acct_id, filepath,
                )
            )
            entries.extend(
                self._emit_contributions(activity["contributions"], acct_id, filepath)
            )
            entries.extend(
                self._emit_fees(activity["fees"], acct_id, filepath)
            )
            entries.extend(
                self._emit_taxes(
                    activity["taxes"], name_to_ticker, acct_id, filepath,
                )
            )
            entries.extend(
                self._emit_balances(holdings, next_day, acct_id, filepath)
            )
            entries.extend(self._emit_prices(holdings, end, filepath, priced))

        return entries

    def _build_cusip_map(self, activity, name_to_ticker):
        """Map CUSIP -> ticker using paired dividend rows."""
        out = {}
        for div in activity["dividends"]:
            ticker = resolve_ticker(div["name"], name_to_ticker)
            if ticker is not None:
                out[div["cusip"]] = ticker
        return out

    def _emit_dividends(
        self, dividends, name_to_ticker, cash_tickers,
        cusip_to_ticker, acct_id, filepath,
    ):
        out = []
        for div in dividends:
            ticker = cusip_to_ticker.get(div["cusip"]) or resolve_ticker(
                div["name"], name_to_ticker
            )
            if ticker is None:
                raise KeyError(
                    f"Could not resolve ticker for dividend {div['name']!r} "
                    f"(account {acct_id}) in {filepath}"
                )
            out.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=div["date"],
                flag="*",
                payee=None,
                narration=f"Dividend - {ticker}",
                tags=frozenset(),
                links=frozenset(),
                postings=[
                    Posting(
                        self.income_account(acct_id, ticker),
                        None, None, None, None, None,
                    ),
                    Posting(
                        self.cash_account(acct_id),
                        Amount(div["amount"], self.currency),
                        None, None, None, None,
                    ),
                ],
            ))
        return out

    def _emit_reinvestments(
        self, reinvestments, cusip_to_ticker, cash_tickers, acct_id, filepath,
    ):
        out = []
        for rv in reinvestments:
            ticker = cusip_to_ticker.get(rv["cusip"])
            if ticker is None:
                raise KeyError(
                    f"Could not resolve ticker for reinvestment CUSIP "
                    f"{rv['cusip']} (account {acct_id}) in {filepath}"
                )
            if ticker in cash_tickers:
                # Sweep into the money market fund; already represented
                # by the dividend posting to cash.
                continue
            out.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=rv["date"],
                flag="*",
                payee=None,
                narration=f"Reinvest {ticker}",
                tags=frozenset(),
                links=frozenset(),
                postings=[
                    Posting(
                        self.cash_account(acct_id),
                        # Row amount is the magnitude; the buy debits cash.
                        Amount(-rv["amount"], self.currency),
                        None, None, None, None,
                    ),
                    Posting(
                        self.sub_account(acct_id, ticker),
                        Amount(rv["quantity"], ticker),
                        CostSpec(rv["price"], None, self.currency, None, None, None),
                        None, None, None,
                    ),
                ],
            ))
        return out

    def _emit_trades(
        self, trades, name_to_ticker, cash_tickers, acct_id, filepath,
    ):
        out = []
        for tr in trades:
            ticker = resolve_ticker(tr["name"], name_to_ticker)
            if ticker is None:
                # Fully-sold-out positions don't appear in holdings, so
                # fall back to the first token of the security name when
                # it looks like a stock symbol (1-5 uppercase letters).
                first = tr["name"].split(" ", 1)[0]
                if re.fullmatch(r"[A-Z]{1,5}", first):
                    ticker = first
            if ticker is None:
                raise KeyError(
                    f"Could not resolve ticker for trade {tr['name']!r} "
                    f"(account {acct_id}) in {filepath}"
                )
            if ticker in cash_tickers:
                # Pure cash-sweep movement; cash side already reflected
                # by the paired dividend/contribution/fee posting.
                continue
            is_buy = tr["side"] == "Bought"
            qty = tr["quantity"]
            price = tr["price"]
            amount = tr["amount"]
            if is_buy:
                postings = [
                    Posting(
                        self.cash_account(acct_id),
                        Amount(-amount, self.currency),
                        None, None, None, None,
                    ),
                    Posting(
                        self.sub_account(acct_id, ticker),
                        Amount(qty, ticker),
                        CostSpec(price, None, self.currency, None, None, None),
                        None, None, None,
                    ),
                ]
            else:
                postings = [
                    Posting(
                        self.cash_account(acct_id),
                        Amount(amount, self.currency),
                        None, None, None, None,
                    ),
                    Posting(
                        self.sub_account(acct_id, ticker),
                        Amount(-qty, ticker),
                        CostSpec(None, None, None, None, None, None),
                        Amount(price, self.currency),
                        None, None,
                    ),
                    Posting(
                        self.income_account(acct_id, ticker),
                        None, None, None, None, None,
                    ),
                ]
            out.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=tr["date"],
                flag="*",
                payee=None,
                narration=f"{'Buy' if is_buy else 'Sell'} {ticker}",
                tags=frozenset(),
                links=frozenset(),
                postings=postings,
            ))
        return out

    def _emit_contributions(self, contributions, acct_id, filepath):
        equity = self._accounts[acct_id].get("equity_account")
        out = []
        for c in contributions:
            postings = [
                Posting(
                    self.cash_account(acct_id),
                    Amount(c["amount"], self.currency),
                    None, None, None, None,
                ),
            ]
            if equity:
                postings.append(
                    Posting(equity, None, None, None, None, None)
                )
            out.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=c["date"],
                flag="*",
                payee=None,
                narration=f"Contribution - {c['description']}",
                tags=frozenset(),
                links=frozenset(),
                postings=postings,
            ))
        return out

    def _emit_fees(self, fees, acct_id, filepath):
        expenses = self._accounts[acct_id].get("expense_account")
        out = []
        for f in fees:
            amt = -f["amount"]  # fees parsed as positive magnitude after "-"
            postings = [
                Posting(
                    self.cash_account(acct_id),
                    Amount(amt, self.currency),
                    None, None, None, None,
                ),
            ]
            if expenses:
                postings.append(
                    Posting(expenses, None, None, None, None, None)
                )
            out.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=f["date"],
                flag="*",
                payee=PAYEE_FIDELITY,
                narration=f["description"],
                tags=frozenset(),
                links=frozenset(),
                postings=postings,
            ))
        return out

    def _emit_taxes(self, taxes, name_to_ticker, acct_id, filepath):
        expenses = self._accounts[acct_id].get("expense_account")
        out = []
        for t in taxes:
            amt = -t["amount"]
            ticker = resolve_ticker(t["name"], name_to_ticker)
            narration = f"{t['kind']} - {ticker}" if ticker else t["kind"]
            postings = [
                Posting(
                    self.cash_account(acct_id),
                    Amount(amt, self.currency),
                    None, None, None, None,
                ),
            ]
            if expenses:
                postings.append(
                    Posting(expenses, None, None, None, None, None)
                )
            out.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=t["date"],
                flag="*",
                payee=PAYEE_FIDELITY,
                narration=narration,
                tags=frozenset(),
                links=frozenset(),
                postings=postings,
            ))
        return out

    def _emit_summary(self, lines, activity, end, next_day, acct_id, filepath):
        """Single Change-in-Value txn + fee txns + ending balance, no leaves."""
        cfg = self._accounts[acct_id]
        base = cfg["account"]
        summary = parse_summary(lines)
        if "change_in_value" not in summary or "ending_value" not in summary:
            raise KeyError(
                f"Missing Change in Investment Value / Ending Account Value "
                f"for summary account {acct_id} in {filepath}"
            )

        out = []
        change = summary["change_in_value"]
        out.append(Transaction(
            meta=new_metadata(filepath, 0),
            date=end,
            flag="*",
            payee=None,
            narration="Change in Investment Value",
            tags=frozenset(),
            links=frozenset(),
            postings=[
                Posting(
                    base,
                    Amount(change, self.currency),
                    None, None, None, None,
                ),
                Posting(
                    cfg["income_base"], None, None, None, None, None,
                ),
            ],
        ))

        expenses = cfg.get("expense_account")
        for f in activity["fees"]:
            amt = -f["amount"]
            postings = [
                Posting(base, Amount(amt, self.currency), None, None, None, None),
            ]
            if expenses:
                postings.append(Posting(expenses, None, None, None, None, None))
            out.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=f["date"],
                flag="*",
                payee=PAYEE_FIDELITY,
                narration=f["description"],
                tags=frozenset(),
                links=frozenset(),
                postings=postings,
            ))

        for t in activity["taxes"]:
            amt = -t["amount"]
            postings = [
                Posting(base, Amount(amt, self.currency), None, None, None, None),
            ]
            if expenses:
                postings.append(Posting(expenses, None, None, None, None, None))
            out.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=t["date"],
                flag="*",
                payee=PAYEE_FIDELITY,
                narration=t["kind"],
                tags=frozenset(),
                links=frozenset(),
                postings=postings,
            ))

        out.append(Balance(
            meta=new_metadata(filepath, 0),
            date=next_day,
            account=base,
            amount=Amount(summary["ending_value"], self.currency),
            tolerance=None,
            diff_amount=None,
        ))
        return out

    def _emit_prices(self, holdings, end, filepath, priced):
        out = []
        for ticker, h in holdings.items():
            if h["is_cash"]:
                # The sweep fund is booked as USD, so its $1.00 quote is
                # not a price for any commodity in the ledger.
                continue
            if ticker in priced:
                # Same fund held in more than one account on the statement.
                continue
            priced.add(ticker)
            out.append(Price(
                meta=new_metadata(filepath, 0),
                date=end,
                currency=ticker,
                amount=Amount(h["price"], self.currency),
            ))
        return out

    def _emit_balances(self, holdings, next_day, acct_id, filepath):
        out = []
        for ticker, h in holdings.items():
            if h["is_cash"]:
                out.append(Balance(
                    meta=new_metadata(filepath, 0),
                    date=next_day,
                    account=self.cash_account(acct_id),
                    amount=Amount(h["ending_value"], self.currency),
                    tolerance=None,
                    diff_amount=None,
                ))
            else:
                out.append(Balance(
                    meta=new_metadata(filepath, 0),
                    date=next_day,
                    account=self.sub_account(acct_id, ticker),
                    amount=Amount(h["quantity"], ticker),
                    tolerance=None,
                    diff_amount=None,
                ))
        return out
