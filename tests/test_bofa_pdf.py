"""Tests for the Bank of America deposit account PDF importer."""
from datetime import date
from decimal import Decimal

import pytest
from beancount.core.data import Amount, Balance, Posting, Transaction, new_metadata
from beangulp.extract import DUPLICATE

from beancount_utils.decorator import Decoration, Decorator
from beancount_utils.importers import bofa_pdf as mod


ACCOUNT = "Assets:US:BofA:Checking"

PAGE_1 = """\
Customer service information
JANE DOE bankofamerica.com
Bank of America, N.A.
Your Adv Plus Banking
for July 16, 2026 to August 13, 2026 Account number: 0000 1111 2222
Account summary
Beginning balance on July 16, 2026 $1,000.00
Deposits and other additions 2,150.00
ATM and debit card subtractions -25.00
Other subtractions -1,120.00
Checks -100.00
Service fees -0.00
Ending balance on August 13, 2026 $1,905.00
Cash back deals are better than ever.
Page 1 of 4
"""

PAGE_2 = """\
JANE DOE ! Account # 0000 1111 2222 ! July 16, 2026 to August 13, 2026
Deposits and other additions
Date Description Amount
07/24/26 ACME CORP DES:PAYROLL ID:111 INDN:DOE,JANE CO 2,000.00
ID:222 PPD
08/03/26 Online Banking transfer from SAV 9999 Confirmation# abc123 150.00
Total deposits and other additions $2,150.00
Withdrawals and other subtractions
ATM and debit card subtractions
Date Description Amount
Card account # XXXX XXXX XXXX 4321
07/20/26 CHECKCARD 0719 COFFEE SHOP ANYTOWN NC -25.00
Subtotal for card account # XXXX XXXX XXXX 4321 -$25.00
Total ATM and debit card subtractions -$25.00
Other subtractions
Date Description Amount
07/16/26 Zelle payment to JOHN SMITH Conf# xyz789 -420.00
continued on the next page
Make bank transfers in the Mobile Banking app
Scan the code 12.34
Page 2 of 4
"""

PAGE_3 = """\
JANE DOE ! Account # 0000 1111 2222 ! July 16, 2026 to August 13, 2026
Withdrawals and other subtractions - continued
Other subtractions - continued
Date Description Amount
07/18/26 UTILITY CO DES:PAYMENT ID:333 INDN:JANE DOE CO -700.00
ID:444 WEB
Total other subtractions -$1,120.00
Checks
Date Check # Amount Date Check # Amount
07/22/26 101 -60.00 07/29/26 103* -40.00
Total checks -$100.00
Braille and Large Print Request 800.432.1000
Page 3 of 4
"""

PAGE_4 = """\
JANE DOE ! Account # 0000 1111 2222 ! July 16, 2026 to August 13, 2026
This page intentionally left blank
Page 4 of 4
"""

PAGES = [PAGE_1, PAGE_2, PAGE_3, PAGE_4]


@pytest.fixture
def importer():
    return mod.Importer(account=ACCOUNT, account_number="2222")


@pytest.fixture
def pages(monkeypatch):
    pages = list(PAGES)
    monkeypatch.setattr(mod, "pdf_to_pages", lambda path: pages)
    monkeypatch.setattr(mod, "pdf_first_page", lambda path: pages[0])
    return pages


@pytest.fixture
def entries(importer, pages):
    return importer.extract("statement.pdf", [])


def transactions(entries):
    return [e for e in entries if isinstance(e, Transaction)]


def test_period_and_summary():
    assert mod.parse_period(PAGE_1) == (date(2026, 7, 16), date(2026, 8, 13))
    assert mod.parse_summary(PAGE_1.splitlines()) == {
        "beginning": Decimal("1000.00"),
        "ending": Decimal("1905.00"),
    }


def test_rows_from_every_table(entries):
    got = [(t.date, t.postings[0].units) for t in transactions(entries)]
    assert got == [
        (date(2026, 7, 16), Amount(Decimal("-420.00"), "USD")),
        (date(2026, 7, 18), Amount(Decimal("-700.00"), "USD")),
        (date(2026, 7, 20), Amount(Decimal("-25.00"), "USD")),
        (date(2026, 7, 22), Amount(Decimal("-60.00"), "USD")),
        (date(2026, 7, 24), Amount(Decimal("2000.00"), "USD")),
        (date(2026, 7, 29), Amount(Decimal("-40.00"), "USD")),
        (date(2026, 8, 3), Amount(Decimal("150.00"), "USD")),
    ]
    assert all(t.postings[0].account == ACCOUNT for t in transactions(entries))


def test_wrapped_description_is_joined(entries):
    payroll = next(t for t in transactions(entries) if t.date == date(2026, 7, 24))
    assert payroll.narration == "ACME CORP DES:PAYROLL ID:111 INDN:DOE,JANE CO ID:222 PPD"


def test_wrap_across_page_break_is_not_polluted_by_footer(entries):
    zelle = next(t for t in transactions(entries) if t.date == date(2026, 7, 16))
    assert zelle.narration == "Zelle payment to JOHN SMITH Conf# xyz789"


@pytest.mark.parametrize("description, payee", [
    ("ACME CORP DES:PAYROLL ID:111 INDN:DOE,JANE CO ID:222 PPD", "ACME CORP"),
    ("Zelle payment to JOHN SMITH Conf# xyz789", "Zelle payment to JOHN SMITH"),
    ("Online Banking transfer from SAV 9999 Confirmation# abc123",
     "Online Banking transfer from SAV 9999"),
    ("Check 101", "Check 101"),
])
def test_payee(description, payee):
    assert mod.payee_of(description) == payee


def test_checks_table_with_two_column_groups(entries):
    checks = [t.narration for t in transactions(entries) if t.narration.startswith("Check ")]
    assert checks == ["Check 101", "Check 103"]


def test_ending_balance_asserted_day_after_period(entries):
    balances = [e for e in entries if isinstance(e, Balance)]
    assert len(balances) == 1
    assert balances[0].date == date(2026, 8, 14)
    assert balances[0].account == ACCOUNT
    assert balances[0].amount == Amount(Decimal("1905.00"), "USD")


def test_unreconciled_statement_is_rejected(importer, pages):
    pages[2] = pages[2].replace("-700.00", "-70.00")
    with pytest.raises(ValueError, match="balances changed"):
        importer.extract("statement.pdf", [])


@pytest.mark.parametrize("number, expected", [
    ("2222", True),
    ("0000 1111 2222", True),
    ("3333", False),
])
def test_identify_by_account_number(monkeypatch, pages, number, expected):
    monkeypatch.setattr(mod.mimetypes, "guess_type", lambda path: ("application/pdf", None))
    assert mod.Importer(ACCOUNT, number).identify("statement.pdf") is expected


def test_identify_rejects_other_banks(monkeypatch, importer, pages):
    monkeypatch.setattr(mod.mimetypes, "guess_type", lambda path: ("application/pdf", None))
    pages[0] = pages[0].replace("Bank of America", "Other Bank")
    assert not importer.identify("statement.pdf")


def test_date_is_period_end(importer, pages):
    assert importer.date("statement.pdf") == date(2026, 8, 13)


def test_deduplicate_marks_existing_transactions(importer, entries):
    existing = [Transaction(
        new_metadata("ledger.beancount", 1), date(2026, 7, 25), "*",
        "ACME", "Payroll", frozenset(), frozenset(),
        [Posting(ACCOUNT, Amount(Decimal("2000.00"), "USD"), None, None, None, None)],
    )]
    importer.deduplicate(entries, existing)
    dupes = [e for e in transactions(entries) if DUPLICATE in e.meta]
    assert [d.date for d in dupes] == [date(2026, 7, 24)]


def ledger_txn(day, number, account=ACCOUNT):
    return Transaction(
        new_metadata("ledger.beancount", 1), day, "*",
        "Someone", "", frozenset(), frozenset(),
        [Posting(account, Amount(Decimal(number), "USD"), None, None, None, None)],
    )


def out_of_place(entries):
    return [e for e in transactions(entries) if "OUT_OF_PLACE" in e.tags]


def test_extra_ledger_transaction_is_out_of_place(importer, entries):
    payroll = ledger_txn(date(2026, 7, 24), "2000.00")
    extra = ledger_txn(date(2026, 8, 13), "-55.00")
    importer.deduplicate(entries, [payroll, extra])
    oop = out_of_place(entries)
    assert [(e.date, e.postings[0].units.number) for e in oop] == [
        (date(2026, 8, 13), Decimal("-55.00")),
    ]
    assert oop[0].postings[0].flag == "!"


@pytest.mark.parametrize("day, account", [
    pytest.param(date(2026, 7, 15), ACCOUNT, id="before-period"),
    pytest.param(date(2026, 8, 14), ACCOUNT, id="after-period"),
    pytest.param(date(2026, 8, 1), "Assets:US:BofA:Savings", id="other-account"),
])
def test_ledger_transactions_outside_statement_are_ignored(importer, entries, day, account):
    importer.deduplicate(entries, [ledger_txn(day, "-55.00", account)])
    assert out_of_place(entries) == []


def test_out_of_place_entries_are_not_decorated(importer, entries):
    importer.decorator = Decorator([Decoration({"re": ".", "target_account": "Expenses:Misc"})])
    importer.deduplicate(entries, [ledger_txn(date(2026, 8, 1), "-55.00")])
    (oop,) = out_of_place(entries)
    assert len(oop.postings) == 1
