"""
Extract beancount entries from a Merrill Edge brokerage statement PDF.

Emits:
  - one Buy transaction per "Purchase" row in SECURITY TRANSACTIONS
  - one Sell transaction per "Sale" row
  - one Sell-at-par transaction per "Redemption" row (e.g. matured bonds)
  - one Dividend/Interest transaction per row in DIVIDENDS/INTEREST INCOME TRANSACTIONS
  - one Deposit/Withdrawal per Electronic Transfer row
  - one balance assertion per held commodity at period-end + 1 day
    (cash held in the sweep account asserts in `currency`)

Account layout:
  Assets:               {account}:{COMMODITY}      (cash uses :USD)
  Income (dividends):   {income_base}:{COMMODITY}

Bonds use a synthetic commodity name of "B" + CUSIP (e.g. B00130HCE3).
For ETPs whose ticker isn't visible at end-of-period (because they were fully
sold during the period), pass a `cusip_overrides` map to the constructor.

To dump pages for importer development, use:
  python -c "from beancount_utils.importers.merrill_pdf import pdf_to_pages; \
    print('\n\n===PAGE BREAK===\n\n'.join(pdf_to_pages('path/to/statement.pdf')))" > /tmp/merrill_dump.txt
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

SYMBOL_RE = re.compile(r"SYMBOL:\s+([A-Z0-9]{1,6})")
CUSIP_HOLD_RE = re.compile(r"CUSIP:\s+([0-9A-Z]{9})")
CUSIP_NUM_RE = re.compile(r"CUSIP\s*NUM:\s+([0-9A-Z]{9})")
CUS_NO_RE = re.compile(r"CUS\s*NO\s+([0-9A-Z]{9})")
UNIT_PRICE_RE = re.compile(r"UNIT\s+PRICE\s+([\d,]+\.\d{2,6})")

# ETP holding data row: qty cost price mv unrl inv ret income (8 numbers)
HOLDING_ETP_RE = re.compile(
    r"([\d,]+\.\d{1,4})\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2,4})\s+"
    r"([\d,]+\.\d{2})\s+"
    r"\(?-?[\d,]+\.\d{2}\)?\s+"
    r"[\d,]+\s+"
    r"\(?-?[\d,]+\)?\s+"
    r"[\d,]+\s*$"
)

# Fixed income holding row: acquired_date qty cost price mv unrl
HOLDING_FI_RE = re.compile(
    r"(\d{2}/\d{2}/\d{2})\s+"
    r"([\d,]+)\s+"
    r"([\d,]+\.\d{2})\s+"
    r"([\d,]+\.\d{2,4})\s+"
    r"([\d,]+\.\d{2})\s+"
    r"\(?-?([\d,]+\.\d{2})\)?\s*$"
)

# Date normalization: pdfplumber sometimes renders "01/30" as "0 1 . /30"
DATE_FIX_RE = re.compile(r"^(\d)\s*(\d)\s*\.\s*/(\d{2})\b")

# Security transaction headline: date name (Purchase|Sale|Redemption) qty ...amounts
TRADE_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)\s+(Purchase|Sale|Redemption)\s+(.+)$"
)

# Dividend/interest row: date name (Interest|Bank Interest|Dividend) amount
INTEREST_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)\s+(Bank Interest|Interest|Dividend)\s+([\d,]*\.\d{2})\s*$"
)

# Electronic transfer row
TRANSFER_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(.+?)\s+(Withdrawal|Funds Received)\s+([\d,]+\.\d{2})\s*$"
)


def to_decimal(s: str) -> Decimal:
    s = s.replace("$", "").replace(",", "").strip()
    if s.startswith("(") and s.endswith(")"):
        return -Decimal(s[1:-1])
    return Decimal(s)


def normalize_line(line: str) -> str:
    return DATE_FIX_RE.sub(r"\1\2/\3", line.strip())


def clean_name(name: str) -> str:
    """Strip Merrill's stylized-text spacing artifacts (e.g. "IS H A R. ES" -> "ISHARES")."""
    # Collapse runs of single-letter tokens (with optional embedded ". ")
    parts = re.split(r"(\s+)", name)
    out = []
    buf = []
    for p in parts:
        if re.fullmatch(r"[A-Z]\.?", p):
            buf.append(p.replace(".", ""))
        elif p.strip() == "":
            if buf:
                continue
            out.append(p)
        else:
            if buf:
                # If next token starts with a lowercase or continues the word, merge.
                # Use heuristic: glue letter cluster onto next token if next token is alpha
                if re.match(r"[A-Za-z]", p):
                    out.append("".join(buf) + p)
                else:
                    out.append("".join(buf))
                    out.append(p)
                buf = []
            else:
                out.append(p)
    if buf:
        out.append("".join(buf))
    return re.sub(r"\s+", " ", "".join(out)).strip()


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
    with pdfplumber.open(filepath) as pdf:
        if not pdf.pages:
            return ""
        return pdf.pages[0].extract_text() or ""


def resolve_year(month: int, start: date, end: date) -> int:
    if start.year == end.year:
        return start.year
    return start.year if month >= start.month else end.year


def flatten(pages: list[str]) -> list[str]:
    out: list[str] = []
    for text in pages:
        out.extend(text.splitlines())
    return out


def parse_holdings(lines: list[str]) -> tuple[dict[str, dict], Decimal]:
    """Return (holdings, cash_total).

    holdings keyed by commodity (ticker for ETPs, B<cusip> for bonds).
    Each value: {name, quantity, ending_value, kind in {"etp","bond"}}.
    """
    holdings: dict[str, dict] = {}
    cash_total = Decimal(0)
    section = None

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if "CASH/MONEY ACCOUNTS" in line:
            section = "cash"
        elif "GOVERNMENT AND AGENCY SECURITIES" in line or "CORPORATE BONDS" in line:
            section = "fi"
        elif "MUTUAL FUNDS" in line and "ETPs" in line:
            section = "etp"
        elif (
            line.startswith("Subtotal")
            or line.startswith("LONG PORTFOLIO")
            or line.startswith("YOUR CMA TRANSACTIONS")
        ):
            section = None

        if section == "cash" and line.startswith("TOTAL "):
            m = re.match(r"TOTAL\s+([\d,]+\.\d{2})\b", line)
            if m:
                cash_total = to_decimal(m.group(1))
            section = None
            i += 1
            continue

        if section == "etp":
            m = HOLDING_ETP_RE.search(line)
            if m and i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                sym_m = SYMBOL_RE.search(next_line)
                if sym_m:
                    ticker = sym_m.group(1)
                    name1 = clean_name(line[: m.start()].rstrip())
                    cont_m = re.match(r"^(.+?)\s+CURRENT YIELD", next_line)
                    name2 = clean_name(cont_m.group(1)) if cont_m else ""
                    full_name = f"{name1} {name2}".strip()
                    qty = to_decimal(m.group(1))
                    mv = to_decimal(m.group(4))
                    holdings[ticker] = {
                        "name": full_name,
                        "quantity": qty,
                        "ending_value": mv,
                        "kind": "etp",
                    }
                    i += 2
                    continue

        if section == "fi":
            m = HOLDING_FI_RE.search(line)
            if m and i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                cusip_m = CUSIP_HOLD_RE.search(next_line)
                if cusip_m:
                    cusip = cusip_m.group(1)
                    commodity = f"B{cusip}"
                    name = clean_name(line[: m.start()].rstrip())
                    qty = to_decimal(m.group(2))
                    mv = to_decimal(m.group(5))
                    holdings[commodity] = {
                        "name": name,
                        "quantity": qty,
                        "ending_value": mv,
                        "kind": "bond",
                    }
                    i += 2
                    continue

        i += 1

    return holdings, cash_total


def build_name_to_cusip(lines: list[str]) -> dict[str, str]:
    """Walk every line; any "CUSIP[ NUM]:" pattern attributes the cusip to the
    name on the preceding line (trimming leading dates and trailing transaction
    type or amounts)."""
    name_to_cusip: dict[str, str] = {}
    for i, raw in enumerate(lines):
        line = raw.strip()
        m = CUSIP_NUM_RE.search(line) or CUSIP_HOLD_RE.search(line)
        if not m or i == 0:
            continue
        prev = normalize_line(lines[i - 1])
        # Strip leading "MM/DD " if present
        prev = re.sub(r"^\d{2}/\d{2}\s+", "", prev)
        # Trim at first transaction-type keyword or quantity
        prev = re.split(
            r"\s+(?:Interest|Bank Interest|Dividend|Purchase|Sale|Redemption)\b",
            prev,
        )[0]
        prev = re.sub(r"\s+-?\(?[\d,]+\.\d{2,4}\)?.*$", "", prev)
        name = clean_name(prev)
        if name:
            name_to_cusip.setdefault(name, m.group(1))
    return name_to_cusip


def parse_security_transactions(lines: list[str], start: date, end: date) -> list[dict]:
    out: list[dict] = []
    in_section = False
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        if stripped.startswith("SECURITY TRANSACTIONS"):
            in_section = True
            i += 1
            continue
        if in_section and (
            stripped.startswith("CASH/OTHER")
            or stripped.startswith("REALIZED GAINS")
            or stripped.startswith("YOUR CMA MONEY")
            or stripped.startswith("DIVIDENDS/INTEREST")
            or stripped.startswith("TOTAL SECURITY")
            or stripped.startswith("Please see the Realized")
        ):
            in_section = False
        if not in_section:
            i += 1
            continue

        m = TRADE_RE.match(stripped)
        if not m:
            i += 1
            continue
        mm_dd, name, side, rest = m.groups()
        nums = re.findall(r"\(?-?[\d,]+\.\d{2,4}\)?", rest)
        if not nums:
            i += 1
            continue
        qty = to_decimal(nums[0])
        cash = to_decimal(nums[-1])

        cusip = None
        price = None
        name_continuation = ""
        for j in range(i + 1, min(i + 3, len(lines))):
            next_line = lines[j].strip()
            cm = CUS_NO_RE.search(next_line)
            pm = UNIT_PRICE_RE.search(next_line)
            if cm:
                cusip = cm.group(1)
                cont_m = re.match(r"^(.+?)\s+CUS\s*NO", next_line)
                if cont_m:
                    name_continuation = clean_name(cont_m.group(1))
            if pm:
                price = to_decimal(pm.group(1))
            if cm or pm:
                break

        month, day = (int(x) for x in mm_dd.split("/"))
        full_name = clean_name(f"{name.strip()} {name_continuation}".strip())
        out.append({
            "date": date(resolve_year(month, start, end), month, day),
            "name": full_name,
            "side": side,
            "quantity": abs(qty),
            "cash": cash,
            "cusip": cusip,
            "price": price,
        })
        i += 1
    return out


def parse_dividends(lines: list[str], start: date, end: date) -> list[dict]:
    out: list[dict] = []
    in_section = False
    for raw in lines:
        line = normalize_line(raw)
        if "DIVIDENDS/INTEREST INCOME TRANSACTIONS" in line:
            in_section = True
            continue
        if in_section and (
            line.startswith("SECURITY TRANSACTIONS")
            or line.startswith("NET TOTAL")
        ):
            in_section = False
        if not in_section:
            continue
        m = INTEREST_RE.match(line)
        if not m:
            continue
        mm_dd, name, kind, amount = m.groups()
        # Strip Merrill's IRS-reporting markers ("*", ":", "#") from the tail.
        name = re.sub(r"\s+[\*:#]+\s*$", "", name)
        month, day = (int(x) for x in mm_dd.split("/"))
        out.append({
            "date": date(resolve_year(month, start, end), month, day),
            "name": clean_name(name.strip()),
            "kind": kind,
            "amount": to_decimal(amount),
        })
    return out


def parse_transfers(lines: list[str], start: date, end: date) -> list[dict]:
    out: list[dict] = []
    in_section = False
    for raw in lines:
        stripped = raw.strip()
        if stripped == "Electronic Transfers":
            in_section = True
            continue
        if in_section and (
            stripped.startswith("Subtotal")
            or stripped.startswith("NET TOTAL")
            or stripped.startswith("YOUR CMA MONEY")
        ):
            in_section = False
        if not in_section:
            continue
        m = TRANSFER_RE.match(stripped)
        if not m:
            continue
        mm_dd, description, kind, amount = m.groups()
        month, day = (int(x) for x in mm_dd.split("/"))
        amt = to_decimal(amount)
        if kind == "Withdrawal":
            amt = -amt
        out.append({
            "date": date(resolve_year(month, start, end), month, day),
            "description": description.strip(),
            "amount": amt,
        })
    return out


class Importer(beangulp.Importer):
    """An importer for Merrill Edge brokerage statement PDFs."""

    def __init__(
        self,
        account: str,
        income_base: str | None = None,
        account_id: str | None = None,
        currency: str = "USD",
        cusip_overrides: dict[str, str] | None = None,
    ):
        self._account_base = account
        self.income_base = (
            income_base if income_base is not None else account.replace("Assets", "Income", 1)
        )
        self.account_id = account_id
        self.currency = currency
        self.cusip_overrides = cusip_overrides or {}

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
        if "Merrill" not in text and "MERRILL" not in text and "MLPF&S" not in text:
            return False
        if self.account_id and self.account_id not in text:
            return False
        return PERIOD_RE.search(text) is not None

    def account(self, filepath):
        return self._account_base

    def _resolve_commodity(
        self,
        name: str,
        cusip: str | None,
        side: str,
        name_to_ticker: dict[str, str],
        name_to_cusip: dict[str, str],
    ) -> str:
        if cusip and cusip in self.cusip_overrides:
            return self.cusip_overrides[cusip]
        if side == "Redemption":
            c = cusip or name_to_cusip.get(name)
            if not c:
                raise KeyError(f"No CUSIP for redemption {name!r}")
            return f"B{c}"
        if name in name_to_ticker:
            return name_to_ticker[name]
        # Match normalized name against normalized ticker items
        norm = re.sub(r"[^A-Z0-9]", "", name.upper())
        for n, t in name_to_ticker.items():
            n_norm = re.sub(r"[^A-Z0-9]", "", n.upper())
            if norm and (n_norm.startswith(norm) or norm.startswith(n_norm)):
                return t
        if cusip:
            raise KeyError(
                f"Could not resolve commodity for {name!r} (cusip {cusip}); "
                f"add it to cusip_overrides"
            )
        raise KeyError(f"Could not resolve commodity for {name!r}")

    def extract(self, filepath, existing):
        pages = pdf_to_pages(filepath)
        full_text = "\n".join(pages)
        start, end = parse_period(full_text)
        lines = flatten(pages)

        holdings, cash_total = parse_holdings(lines)
        name_to_ticker = {h["name"]: c for c, h in holdings.items() if h["kind"] == "etp"}
        name_to_cusip = build_name_to_cusip(lines)

        entries: list = []

        for div in parse_dividends(lines, start, end):
            if div["kind"] == "Bank Interest":
                commodity = self.currency
            else:
                cusip = name_to_cusip.get(div["name"])
                if cusip:
                    commodity = f"B{cusip}"
                else:
                    commodity = self._resolve_commodity(
                        div["name"], None, "Dividend", name_to_ticker, name_to_cusip
                    )
            entries.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=div["date"],
                flag="*",
                payee=None,
                narration=f"{div['kind']} - {commodity}",
                tags=frozenset(),
                links=frozenset(),
                postings=[
                    Posting(self.income_account(commodity), None, None, None, None, None),
                    Posting(
                        self.cash_account,
                        Amount(div["amount"], self.currency),
                        None, None, None, None,
                    ),
                ],
            ))

        for trade in parse_security_transactions(lines, start, end):
            commodity = self._resolve_commodity(
                trade["name"], trade["cusip"], trade["side"], name_to_ticker, name_to_cusip
            )
            qty = trade["quantity"]
            cash = trade["cash"]
            price = trade["price"]
            if price is None and qty:
                price = abs(cash) / qty

            if trade["side"] == "Purchase":
                postings = [
                    Posting(
                        self.cash_account,
                        Amount(cash, self.currency),
                        None, None, None, None,
                    ),
                    Posting(
                        self.sub_account(commodity),
                        Amount(qty, commodity),
                        CostSpec(price, None, self.currency, None, None, None),
                        None, None, None,
                    ),
                ]
                narration = f"Buy {commodity}"
            else:
                postings = [
                    Posting(
                        self.cash_account,
                        Amount(cash, self.currency),
                        None, None, None, None,
                    ),
                    Posting(
                        self.sub_account(commodity),
                        Amount(-qty, commodity),
                        CostSpec(None, None, None, None, None, None),
                        Amount(price, self.currency) if price is not None else None,
                        None, None,
                    ),
                    Posting(self.income_account(commodity), None, None, None, None, None),
                ]
                narration = f"{'Redeem' if trade['side'] == 'Redemption' else 'Sell'} {commodity}"

            entries.append(Transaction(
                meta=new_metadata(filepath, 0),
                date=trade["date"],
                flag="*",
                payee=None,
                narration=narration,
                tags=frozenset(),
                links=frozenset(),
                postings=postings,
            ))

        for xfer in parse_transfers(lines, start, end):
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
        entries.append(Balance(
            meta=new_metadata(filepath, 0),
            date=next_day,
            account=self.cash_account,
            amount=Amount(cash_total, self.currency),
            tolerance=None,
            diff_amount=None,
        ))
        for commodity, h in holdings.items():
            entries.append(Balance(
                meta=new_metadata(filepath, 0),
                date=next_day,
                account=self.sub_account(commodity),
                amount=Amount(h["quantity"], commodity),
                tolerance=None,
                diff_amount=None,
            ))

        return entries
