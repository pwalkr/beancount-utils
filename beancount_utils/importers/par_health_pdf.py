"""
Extract a paycheck transaction from a Par Health (ADP) earnings statement
PDF.

    Importer(
        account="Assets:US:BofA:Checking",   # where net pay is deposited
        accounts={                           # statement line -> account
            "Regular": "Income:US:ParHealth:Salary",
            "Incentive Comp": "Income:US:ParHealth:Bonus",
            "Social Security Tax": "Expenses:Taxes:US:SocialSecurity",
            "Medicare Tax": "Expenses:Taxes:US:Medicare",
            "Federal Income Tax": "Expenses:Taxes:US:Federal",
            "NC State Income Tax": "Expenses:Taxes:US:NC",
            "Medical": "Expenses:Insurance:Medical",
            "Dental": "Expenses:Insurance:Dental",
            "Vision": "Expenses:Insurance:Vision",
            "HealthSavAcct": "Assets:US:HSA:Cash",
            "401k Def $": "Assets:US:401k:Cash",
        },
        benefits={                           # optional employer benefits
            "Er Hsa": ("Assets:US:HSA:Cash", "Income:US:ParHealth:HSA"),
        },
    )

Text is pulled with pdfplumber's ``extract_text``.  ADP prints amounts
with spaces in place of the thousands separator and decimal point
("2 564 77" is 2,564.77), and the "Other Benefits" column on the right is
run into the same lines as earnings and deductions.  Each line is split
into label/amount segments; amounts are recovered from the digit groups
(1-3 digits, then 3-digit groups, then 2-digit cents).

The importer emits one transaction, dated on the pay date, with a posting
for every earnings, deduction and deposit line that has an amount this
period:
  - Earnings post the negated amount (Income is negative).
  - Deductions and deposits are printed negative and post positive.
  - Lines with only a year-to-date amount are skipped.
  - Deposit lines (e.g. "Checking") post to ``account`` unless mapped in
    ``accounts``.
  - Deductions the statement stars as excluded from federal taxable wages
    get ``pre-tax: TRUE`` posting metadata (``pretax_meta`` renames or,
    as None, disables it).
  - Each ``benefits`` line with an amount this period adds a pair of
    postings: the amount to the first account, its negation to the
    second.  Benefit lines that only print a current amount (e.g.
    "Cur 401K Match") are not supported.

Every line with an amount this period must be mapped; an unmapped one
fails extraction so a new deduction is never silently dropped.  Earnings
are reconciled against gross pay, gross pay less deductions against net
pay, and net pay less deposits against the net check.
"""
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pdfplumber

from beancount.core.data import Amount, Posting, Transaction, new_metadata
import beangulp
from beangulp import mimetypes

from beancount_utils.deduplicate import mark_duplicate_entries


DATE = r"\d{2}/\d{2}/\d{4}"
PAY_DATE_RE = re.compile(rf"Pay Date:\s*({DATE})", re.IGNORECASE)
PERIOD_BEGIN_RE = re.compile(rf"Period Beginning:\s*({DATE})")
PERIOD_END_RE = re.compile(rf"Period Ending:\s*({DATE})")
ADVICE_RE = re.compile(r"Advice number:\s*(\d+)")

# One whitespace-separated piece of an amount: optional sign and dollar
# sign on the leading group, optional "*" (pre-tax) on the cents.
AMOUNT_TOKEN_RE = re.compile(r"^(-)?\$?(\d+)(\*)?$")

EARNINGS_HEADER_RE = re.compile(r"\bthis period\b.*\byear to date\b")


@dataclass
class Segment:
    """A label and the amounts printed after it on one line."""
    label: str
    amounts: list[Decimal]
    pretax: bool = False


def parse_amounts(tokens: list[str]) -> tuple[list[Decimal], bool] | None:
    """Join ADP digit groups back into amounts.

    ["-1", "743", "18*", "28", "237", "32"] -> ([-1743.18, 28237.32], True)

    Returns None when the tokens don't form whole amounts.
    """
    amounts: list[Decimal] = []
    pretax = False
    sign = digits = None
    for token in tokens:
        m = AMOUNT_TOKEN_RE.match(token)
        if not m:
            return None
        minus, group, star = m.groups()
        if digits is None:
            if star or len(group) > 3:
                return None
            sign, digits = minus or "", group
        elif minus:
            return None
        elif len(group) == 3 and not star:
            digits += group
        elif len(group) == 2:
            amounts.append(Decimal(f"{sign}{digits}.{group}"))
            pretax = pretax or bool(star)
            sign = digits = None
        else:
            return None
    if digits is not None:
        return None
    return amounts, pretax


def parse_segments(line: str) -> list[Segment]:
    """Split a line into label/amount segments.

    "Gross Pay $3 200 00 53 000 00 Group Term Life 2 50 37 50" ->
    [Segment("Gross Pay", [3200.00, 53000.00]),
     Segment("Group Term Life", [2.50, 37.50])]

    Segments whose digits don't form whole amounts are dropped.
    """
    raw: list[tuple[list[str], list[str]]] = []
    label: list[str] = []
    tokens: list[str] = []
    for token in line.split():
        if AMOUNT_TOKEN_RE.match(token):
            tokens.append(token)
            continue
        if tokens:
            raw.append((label, tokens))
            label, tokens = [], []
        label.append(token)
    if tokens:
        raw.append((label, tokens))

    segments = []
    for label, tokens in raw:
        parsed = parse_amounts(tokens)
        if label and parsed and parsed[0]:
            segments.append(Segment(" ".join(label), *parsed))
    return segments


def current_earning(segment: Segment) -> Decimal:
    """This period's amount from an earnings or benefits line.

    These lines end with "this period" and "year to date" (earnings may
    lead with rate and hours); a lone amount is year to date only.
    """
    if len(segment.amounts) >= 2:
        return segment.amounts[-2]
    return Decimal(0)


def current_deduction(segment: Segment) -> Decimal:
    """This period's amount from a deduction or deposit line.

    These lines print "this period" then "year to date".  A lone amount
    is this period when negative (a deduction), else year to date only.
    """
    first = segment.amounts[0]
    if len(segment.amounts) >= 2 or first < 0:
        return first
    return Decimal(0)


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%m/%d/%Y").date()


def parse_pay_date(text: str) -> date:
    m = PAY_DATE_RE.search(text)
    if not m:
        raise ValueError("Could not find pay date in PDF")
    return parse_date(m.group(1))


def parse_statement(text: str) -> dict:
    """Parse the earnings statement into its sections.

    Returns {pay_date, period, advice, earnings, gross, deductions,
    net_pay, deposits, net_check, segments}.  Line items are
    (Segment, current amount) pairs; ``segments`` holds every segment on
    the page, for benefit lookups.
    """
    begin = PERIOD_BEGIN_RE.search(text)
    end = PERIOD_END_RE.search(text)
    advice = ADVICE_RE.search(text)
    out: dict = {
        "pay_date": parse_pay_date(text),
        "period": (
            parse_date(begin.group(1)) if begin else None,
            parse_date(end.group(1)) if end else None,
        ),
        "advice": advice.group(1) if advice else None,
        "earnings": [],
        "deductions": [],
        "deposits": [],
        "segments": [],
    }

    section = None  # 'earnings' | 'deductions' | 'deposits' | None
    for line in text.splitlines():
        segments = parse_segments(line)
        out["segments"].extend(segments)
        stripped = line.strip()

        if section is None and "gross" not in out and EARNINGS_HEADER_RE.search(stripped):
            section = "earnings"
            continue
        if stripped.startswith("Gross Pay") and segments:
            out["gross"] = current_earning(segments[0])
            section = "deductions"
            continue
        if stripped.startswith("Net Pay") and segments:
            out["net_pay"] = segments[0].amounts[0]
            section = "deposits"
            continue
        if stripped.startswith("Net Check") and segments:
            out["net_check"] = segments[0].amounts[0]
            section = None
            continue
        if section is None or not segments or not stripped.startswith(segments[0].label):
            continue

        first = segments[0]
        if section == "earnings":
            amount = current_earning(first)
        else:
            amount = current_deduction(first)
        out[section].append((first, amount))

    for key in ("gross", "net_pay"):
        if key not in out:
            raise ValueError(f"Could not find {key.replace('_', ' ')} in PDF")
    out.setdefault("net_check", Decimal(0))
    return out


def reconcile(statement: dict) -> None:
    """Check the parsed lines add up to the statement's totals."""
    def total(section):
        return sum((amount for _, amount in statement[section]), Decimal(0))

    checks = [
        ("Earnings", total("earnings"), "gross pay", statement["gross"]),
        ("Gross pay less deductions", statement["gross"] + total("deductions"),
         "net pay", statement["net_pay"]),
        ("Net pay less deposits", statement["net_pay"] + total("deposits"),
         "net check", statement["net_check"]),
    ]
    for what, got, name, expected in checks:
        if got != expected:
            raise ValueError(
                f"{what} total {got} but the statement's {name} is {expected}"
            )


def pdf_to_text(filepath: str) -> str:
    with pdfplumber.open(filepath) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


class Importer(beangulp.Importer):
    """An importer for Par Health (ADP) earnings statement PDFs."""

    def __init__(
        self,
        account: str,
        accounts: dict[str, str],
        benefits: dict[str, tuple[str, str]] | None = None,
        employer: str = "PAR HEALTH ENTERPRISES",
        payee: str = "Par Health",
        currency: str = "USD",
        flag: str = "*",
        pretax_meta: str | None = "pre-tax",
    ):
        self._account = account
        self.accounts = accounts
        self.benefits = benefits or {}
        self.employer = employer
        self.payee = payee
        self.currency = currency
        self.flag = flag
        self.pretax_meta = pretax_meta

    def identify(self, filepath):
        mimetype, _ = mimetypes.guess_type(filepath)
        if mimetype != "application/pdf":
            return False
        text = pdf_to_text(filepath)
        return self.employer in text and "Earnings Statement" in text

    def account(self, filepath):
        return self._account

    def date(self, filepath):
        return parse_pay_date(pdf_to_text(filepath))

    def extract(self, filepath, existing):
        statement = parse_statement(pdf_to_text(filepath))
        reconcile(statement)

        postings = []
        unmapped = []
        lines = [
            *(("earnings", seg, amount) for seg, amount in statement["earnings"]),
            *(("deductions", seg, amount) for seg, amount in statement["deductions"]),
            *(("deposits", seg, amount) for seg, amount in statement["deposits"]),
        ]
        if statement["net_check"]:
            # Printed positive, unlike deposits; negate to post it the same way.
            lines.append(("deposits", Segment("Net Check", []), -statement["net_check"]))
        for section, segment, amount in lines:
            if not amount:
                continue
            account = self.accounts.get(segment.label)
            if account is None and section == "deposits" and segment.label != "Net Check":
                account = self._account
            if account is None:
                unmapped.append(segment.label)
                continue
            meta = None
            if segment.pretax and self.pretax_meta:
                meta = {self.pretax_meta: True}
            postings.append(self._posting(account, -amount, meta))
        if unmapped:
            raise ValueError(
                f"No account for paystub lines {unmapped} in {filepath}; "
                "add them to `accounts`"
            )
        postings.extend(self._benefit_postings(statement["segments"]))

        start, end = statement["period"]
        narration = "Paycheck"
        if start and end:
            narration = f"Paycheck {start} to {end}"
        meta = new_metadata(filepath, 0)
        if statement["advice"]:
            meta["advice"] = statement["advice"]
        return [Transaction(
            meta=meta,
            date=statement["pay_date"],
            flag=self.flag,
            payee=self.payee,
            narration=narration,
            tags=frozenset(),
            links=frozenset(),
            postings=postings,
        )]

    def _benefit_postings(self, segments):
        postings = []
        for label, (account, source) in self.benefits.items():
            for segment in segments:
                if segment.label == label or segment.label.endswith(" " + label):
                    amount = current_earning(segment)
                    if amount:
                        postings.append(self._posting(account, amount))
                        postings.append(self._posting(source, -amount))
                    break
        return postings

    def _posting(self, account, number, meta=None):
        return Posting(account, Amount(number, self.currency), None, None, None, meta)

    def deduplicate(self, entries, existing):
        # Match on the deposit, so a paycheck already imported from the
        # bank statement is flagged as well as a re-imported paystub.
        mark_duplicate_entries(entries, existing, self._account)
