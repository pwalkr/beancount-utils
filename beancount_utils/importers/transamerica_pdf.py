"""
Extract beancount summary entries from a Transamerica quarterly retirement
statement PDF.

Parses the per-plan "Source" tables (one row per money source), pulls
Credits/Fees, Gain/Loss, and Ending Balance for each configured source, and
emits:
  - one Credits/Fees transaction (dated end-of-period) covering all sources
  - one Gain/Loss transaction (dated end-of-period) covering all sources
  - one balance assertion per source (dated period-end + 1 day)
"""
import re
from datetime import date, timedelta
from decimal import Decimal

import pdfplumber

from beancount.core.data import Amount, Balance, Posting, Transaction, new_metadata
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

MONEY = r"-?\$[\d,]+\.\d{2}"

SOURCE_ROW_RE = re.compile(
    rf"^(?P<name>.+?)\s+"
    rf"(?P<begin>{MONEY})\s+"
    rf"(?P<money_in>{MONEY})\s+"
    rf"(?P<money_out>{MONEY})\s+"
    rf"(?P<transfers>{MONEY})\s+"
    rf"(?P<credits_fees>{MONEY})\s+"
    rf"(?P<gain_loss>{MONEY})\s+"
    rf"(?P<ending>{MONEY})\s+"
    rf"(?P<vested>[\d.]+%)\s*$"
)

DEFAULT_FEES_ACCOUNT = "Expenses:Financial:Fees"
DEFAULT_PNL_ACCOUNT = "Income:Investments:Transamerica:PnL:Pretax"


def to_decimal(money_str: str) -> Decimal:
    return Decimal(money_str.replace("$", "").replace(",", ""))


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


class Importer(beangulp.Importer):
    """An importer for Transamerica retirement statement PDFs.

    `sources` accepts either:
      - a dict mapping source name -> beancount account (all rows share the
        Importer's fees_account / pnl_account), or
      - a list of dicts, each with keys 'source' and 'account', and optional
        per-row overrides 'fees_account' and 'pnl_account'.
    """

    def __init__(
        self,
        sources: dict[str, str] | list[dict],
        fees_account: str = DEFAULT_FEES_ACCOUNT,
        pnl_account: str = DEFAULT_PNL_ACCOUNT,
        currency: str = "USD",
        account_name: str = "Assets:Investments:Transamerica",
    ):
        self.fees_account = fees_account
        self.pnl_account = pnl_account
        self.currency = currency
        self._account = account_name
        self.sources = self._normalize_sources(sources)

    def _normalize_sources(self, sources) -> list[dict]:
        if isinstance(sources, dict):
            return [
                {"source": s, "account": a,
                 "fees_account": self.fees_account,
                 "pnl_account": self.pnl_account}
                for s, a in sources.items()
            ]
        out = []
        for entry in sources:
            out.append({
                "source": entry["source"],
                "account": entry["account"],
                "fees_account": entry.get("fees_account", self.fees_account),
                "pnl_account": entry.get("pnl_account", self.pnl_account),
            })
        return out

    def identify(self, filepath):
        mimetype, _ = mimetypes.guess_type(filepath)
        if mimetype != "application/pdf":
            return False
        text = pdf_first_page(filepath)
        if "Transamerica" not in text:
            return False
        return PERIOD_RE.search(text) is not None

    def account(self, filepath):
        return self._account

    def extract(self, filepath, existing):
        pages = pdf_to_pages(filepath)
        full_text = "\n".join(pages)
        _, period_end = parse_period(full_text)

        parsed = self._parse_rows(pages)

        rows: list[dict] = []
        for cfg in self.sources:
            row = parsed.get(cfg["source"])
            if row is None:
                raise KeyError(
                    f"Source {cfg['source']!r} not found in {filepath}"
                )
            rows.append({**cfg, **row})

        next_day = period_end + timedelta(days=1)
        entries = []

        fees_postings = []
        for r in rows:
            fees_postings.append(Posting(
                r["fees_account"],
                Amount(-r["credits_fees"], self.currency),
                None, None, None, None,
            ))
            fees_postings.append(Posting(
                r["account"],
                Amount(r["credits_fees"], self.currency),
                None, None, None, None,
            ))
        entries.append(Transaction(
            meta=new_metadata(filepath, 0),
            date=period_end,
            flag="*",
            payee=None,
            narration="Recent Activity - Credits/Fees",
            tags=frozenset(),
            links=frozenset(),
            postings=fees_postings,
        ))

        pnl_postings = []
        for r in rows:
            pnl_postings.append(Posting(
                r["pnl_account"],
                Amount(-r["gain_loss"], self.currency),
                None, None, None, None,
            ))
            pnl_postings.append(Posting(
                r["account"],
                Amount(r["gain_loss"], self.currency),
                None, None, None, None,
            ))
        entries.append(Transaction(
            meta=new_metadata(filepath, 0),
            date=period_end,
            flag="*",
            payee=None,
            narration="Market value change",
            tags=frozenset(),
            links=frozenset(),
            postings=pnl_postings,
        ))

        for r in rows:
            entries.append(Balance(
                meta=new_metadata(filepath, 0),
                date=next_day,
                account=r["account"],
                amount=Amount(r["ending"], self.currency),
                tolerance=None,
                diff_amount=None,
            ))

        return entries

    def _parse_rows(self, pages: list[str]) -> dict[str, dict]:
        rows: dict[str, dict] = {}
        for text in pages:
            in_source_table = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("Source ") and "Money In" in stripped:
                    in_source_table = True
                    continue
                if not in_source_table:
                    continue
                if stripped.startswith("Totals"):
                    in_source_table = False
                    continue
                m = SOURCE_ROW_RE.match(stripped)
                if not m:
                    continue
                name = m.group("name").strip()
                rows[name] = {
                    "credits_fees": to_decimal(m.group("credits_fees")),
                    "gain_loss":    to_decimal(m.group("gain_loss")),
                    "ending":       to_decimal(m.group("ending")),
                }
        return rows
