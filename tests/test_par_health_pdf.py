"""Tests for the Par Health (ADP) earnings statement PDF importer."""
from datetime import date
from decimal import Decimal

import pytest
from beancount.core.data import Amount, Posting, Transaction, new_metadata
from beangulp.extract import DUPLICATE

from beancount_utils.importers import par_health_pdf as mod


ACCOUNT = "Assets:US:Bank:Checking"

ACCOUNTS = {
    "Regular": "Income:US:ParHealth:Salary",
    "Incentive Comp": "Income:US:ParHealth:Bonus",
    "Social Security Tax": "Expenses:Taxes:US:SocialSecurity",
    "Medicare Tax": "Expenses:Taxes:US:Medicare",
    "Federal Income Tax": "Expenses:Taxes:US:Federal",
    "NC State Income Tax": "Expenses:Taxes:US:NC",
    "Dental": "Expenses:Insurance:Dental",
    "HealthSavAcct": "Assets:US:HSA:Cash",
    "Medical": "Expenses:Insurance:Medical",
    "Vision": "Expenses:Insurance:Vision",
    "401k Def $": "Assets:US:401k:Cash",
}

# Mirrors pdfplumber's text for an ADP statement: amounts use spaces for
# both separators, and the right-hand "Other Benefits" column runs into
# the earnings and deductions lines.
STATEMENT = """\
===== Page 1/1 =====
\\@[c[ RSVP~(cid:0)(cid:0)(cid:0)C
C S O M . 4 1 F 3 IL 1 E 024 DEPT. CLOCK V 00 C 0 H 0 R 3 . 0 N 08 Earnings Statement
PAR HEALTH ENTERPRISES LLC Period Beginning: 03/02/2026
675 JAMES S MCDONNELL BOULEVARD Period Ending: 03/15/2026
HAZELWOOD, MO 63042 Pay Date: 03/13/2026
JANE Q DOE
Filing Status: Married filing jointly 1 MAIN STREET
Exemptions/Allowances:
ANYTOWN NC 27000
Federal: Optional Higher Withholding Table
rate hours this period year to date Other Benefits and
Regular 40 00 80 00 3 200 00 48 000 00 this period total to date
Incentive Comp 5 000 00 Er Hsa 25 00 375 00
Gross Pay $3 200 00 53 000 00 Group Term Life 2 50 37 50
Totl Hrs Worked 80 00
Statutory Cur 401K Match 160 00
Social Security Tax -180 00 2 700 00 Ytd 401K Match 2 400 00
Medicare Tax -42 10 631 50
NC State Income Tax -95 00 1 425 00
555-555-0100
Federal Income Tax -150 00 2 250 00
Taxable Marital Status:
Dental -12 00* 180 00
NC: Single
HealthSavAcct -200 00* 3 000 00 Exemptions/Allowances:
Medical -60 00* 900 00 NC: 0,Surviving Spouse
Vision -8 00* 120 00
401k Def $ -160 00* 2 400 00
401k Ic 1 200 00
Net Pay $2 292 90
Checking -2 292 90 34 000 00
Net Check $0 00
Excluded from federal taxable wages
Your federal taxable wages this period are
$2 760 00
2000ADP, Inc.
PAR HEALTH ENTERPRISES LLC Advice number: 00000123456
675 JAMES S MCDONNELL BOULEVARD Pay date: 03/13/2026
HAZELWOOD MO 63042
Deposited to the account of account number transit ABA amount
JANE Q DOE xxxxxxxx0000 xxxx xxxx $2 292 90
NON-NEGOTIABLE
"""


@pytest.fixture
def text(monkeypatch):
    holder = {"text": STATEMENT}
    monkeypatch.setattr(mod, "pdf_to_text", lambda path: holder["text"])
    return holder


@pytest.fixture
def importer():
    return mod.Importer(account=ACCOUNT, accounts=dict(ACCOUNTS))


@pytest.fixture
def txn(importer, text):
    (entry,) = importer.extract("paystub.pdf", [])
    return entry


def units(txn):
    return {p.account: p.units.number for p in txn.postings}


@pytest.mark.parametrize("tokens, expected", [
    ("40 00 80 00 3 200 00 48 000 00", (["40.00", "80.00", "3200.00", "48000.00"], False)),
    ("-2 292 90 34 000 00", (["-2292.90", "34000.00"], False)),
    ("$3 200 00", (["3200.00"], False)),
    ("-12 00* 180 00", (["-12.00", "180.00"], True)),
    ("5 000 00", (["5000.00"], False)),
    ("0 05", (["0.05"], False)),
    ("1 234 567 89", (["1234567.89"], False)),
])
def test_parse_amounts(tokens, expected):
    amounts, pretax = mod.parse_amounts(tokens.split())
    assert (amounts, pretax) == ([Decimal(a) for a in expected[0]], expected[1])


@pytest.mark.parametrize("tokens", [
    "1",              # no cents
    "1 234",          # no cents after thousands group
    "1234 00",        # leading group too long
    "12 -34",         # sign inside an amount
    "1 2",            # one-digit group after the leading one
])
def test_parse_amounts_rejects_partial_amounts(tokens):
    assert mod.parse_amounts(tokens.split()) is None


def test_segments_split_run_together_columns():
    segments = mod.parse_segments("Gross Pay $3 200 00 53 000 00 Group Term Life 2 50 37 50")
    assert [(s.label, s.amounts) for s in segments] == [
        ("Gross Pay", [Decimal("3200.00"), Decimal("53000.00")]),
        ("Group Term Life", [Decimal("2.50"), Decimal("37.50")]),
    ]


def test_dollar_sign_label_is_kept():
    (segment,) = mod.parse_segments("401k Def $ -160 00* 2 400 00")
    assert segment.label == "401k Def $"
    assert segment.pretax


def test_transaction_header(txn):
    assert txn.date == date(2026, 3, 13)
    assert txn.payee == "Par Health"
    assert txn.narration == "Paycheck 2026-03-02 to 2026-03-15"
    assert txn.meta["advice"] == "00000123456"
    assert txn.flag == "*"


def test_postings(txn):
    assert units(txn) == {
        "Income:US:ParHealth:Salary": Decimal("-3200.00"),
        "Expenses:Taxes:US:SocialSecurity": Decimal("180.00"),
        "Expenses:Taxes:US:Medicare": Decimal("42.10"),
        "Expenses:Taxes:US:NC": Decimal("95.00"),
        "Expenses:Taxes:US:Federal": Decimal("150.00"),
        "Expenses:Insurance:Dental": Decimal("12.00"),
        "Assets:US:HSA:Cash": Decimal("200.00"),
        "Expenses:Insurance:Medical": Decimal("60.00"),
        "Expenses:Insurance:Vision": Decimal("8.00"),
        "Assets:US:401k:Cash": Decimal("160.00"),
        ACCOUNT: Decimal("2292.90"),
    }
    assert sum(p.units.number for p in txn.postings) == 0


def test_year_to_date_only_lines_are_skipped(txn):
    # "Incentive Comp" has no amount this period and "401k Ic" is unmapped.
    assert "Income:US:ParHealth:Bonus" not in units(txn)


def test_starred_deductions_are_pre_tax(txn):
    pretax = {p.account for p in txn.postings if p.meta and p.meta.get("pre-tax")}
    assert pretax == {
        "Expenses:Insurance:Dental",
        "Assets:US:HSA:Cash",
        "Expenses:Insurance:Medical",
        "Expenses:Insurance:Vision",
        "Assets:US:401k:Cash",
    }


@pytest.mark.parametrize("key, expected", [
    ("pretax", "pretax"),
    (None, None),
])
def test_pretax_meta_option(text, key, expected):
    importer = mod.Importer(ACCOUNT, dict(ACCOUNTS), pretax_meta=key)
    (txn,) = importer.extract("paystub.pdf", [])
    keys = {k for p in txn.postings if p.meta for k in p.meta}
    assert keys == ({expected} if expected else set())


def test_benefits_add_balanced_postings(text):
    importer = mod.Importer(ACCOUNT, dict(ACCOUNTS), benefits={
        "Er Hsa": ("Assets:US:HSA:Cash", "Income:US:ParHealth:HSA"),
        "Group Term Life": ("Expenses:Insurance:Life", "Income:US:ParHealth:Imputed"),
    })
    (txn,) = importer.extract("paystub.pdf", [])
    benefits = [(p.account, p.units.number) for p in txn.postings[-4:]]
    assert benefits == [
        ("Assets:US:HSA:Cash", Decimal("25.00")),
        ("Income:US:ParHealth:HSA", Decimal("-25.00")),
        ("Expenses:Insurance:Life", Decimal("2.50")),
        ("Income:US:ParHealth:Imputed", Decimal("-2.50")),
    ]
    assert sum(p.units.number for p in txn.postings) == 0


def test_mapped_deposit_account(text):
    importer = mod.Importer(ACCOUNT, {**ACCOUNTS, "Checking": "Assets:US:Other:Checking"})
    (txn,) = importer.extract("paystub.pdf", [])
    assert units(txn)["Assets:US:Other:Checking"] == Decimal("2292.90")


def test_unmapped_line_is_rejected(text):
    accounts = {k: v for k, v in ACCOUNTS.items() if k != "Vision"}
    with pytest.raises(ValueError, match="Vision"):
        mod.Importer(ACCOUNT, accounts).extract("paystub.pdf", [])


@pytest.mark.parametrize("old, new, match", [
    pytest.param("3 200 00 48", "3 100 00 48", "gross pay", id="earnings"),
    pytest.param("Vision -8 00*", "Vision -9 00*", "net pay", id="deductions"),
    pytest.param("Checking -2 292 90", "Checking -2 290 90", "net check", id="deposits"),
])
def test_unreconciled_statement_is_rejected(importer, text, old, new, match):
    text["text"] = STATEMENT.replace(old, new)
    with pytest.raises(ValueError, match=match):
        importer.extract("paystub.pdf", [])


def test_paper_check_needs_an_account(importer, text):
    text["text"] = (
        STATEMENT
        .replace("Checking -2 292 90", "Checking -2 000 00")
        .replace("Net Check $0 00", "Net Check $292 90")
    )
    with pytest.raises(ValueError, match="Net Check"):
        importer.extract("paystub.pdf", [])
    importer.accounts["Net Check"] = "Assets:Cash:Checks"
    (txn,) = importer.extract("paystub.pdf", [])
    assert units(txn)["Assets:Cash:Checks"] == Decimal("292.90")
    assert sum(p.units.number for p in txn.postings) == 0


@pytest.mark.parametrize("old, new, expected", [
    pytest.param("", "", True, id="match"),
    pytest.param("PAR HEALTH ENTERPRISES", "OTHER EMPLOYER", False, id="other-employer"),
    pytest.param("Earnings Statement", "Statement", False, id="other-document"),
])
def test_identify(monkeypatch, importer, text, old, new, expected):
    monkeypatch.setattr(mod.mimetypes, "guess_type", lambda path: ("application/pdf", None))
    text["text"] = STATEMENT.replace(old, new) if old else STATEMENT
    assert importer.identify("paystub.pdf") is expected


def test_date_is_pay_date(importer, text):
    assert importer.date("paystub.pdf") == date(2026, 3, 13)


def test_deduplicate_matches_bank_deposit(importer, txn):
    deposit = Transaction(
        new_metadata("ledger.beancount", 1), date(2026, 3, 13), "*",
        "PAR HEALTH", "Payroll", frozenset(), frozenset(),
        [Posting(ACCOUNT, Amount(Decimal("2292.90"), "USD"), None, None, None, None)],
    )
    entries = [txn]
    importer.deduplicate(entries, [deposit])
    assert DUPLICATE in entries[0].meta
