"""Tests for the Chase credit card PDF importer."""
from datetime import date
from decimal import Decimal

import pytest
from beancount.core.data import Amount, Balance, Posting, Transaction, new_metadata
from beangulp.extract import DUPLICATE

from beancount_utils.decorator import Decoration, Decorator
from beancount_utils.importers import chase_pdf as mod


ACCOUNT = "Liabilities:US:Chase:Visa"

PAGE_1 = """\
Manage your account online: Customer Service: MMoobbiillee:: DDoowwnnllooaadd tthhee
www.chase.com/amazon 1-888-247-4080 CChhaassee MMoobbiillee®® aapppp ttooddaayy
New Balance
AACCCCOOUUNNTT SSUUMMMMAARRYY
Account Number: XXXX XXXX XXXX 1234
Previous Balance $500.00
Payment, Credits -$500.00
Purchases +$135.00
Cash Advances $0.00
Fees Charged $0.00
Interest Charged $2.50
New Balance $137.50
Opening/Closing Date 12/27/25 - 01/26/26
Credit Access Line $7,000
0000001 FIS33339 D 5 Y 9 26 26/01/26 Page 1 of 3 06654 MA MA 02336
Messages for details. New Balance: $137.50
Account number: XXXX XXXX XXXX 1234
"""

PAGE_2 = """\
AACCCCOOUUNNTT AACCTTIIVVIITTYY
Date of
Transaction Merchant Name or Transaction Description $ Amount
PAYMENTS AND OTHER CREDITS
12/29 STATEMENT CREDIT -100.00
01/23 AUTOMATIC PAYMENT - THANK YOU -400.00
PURCHASE
12/30 AMAZON MKTPL*B563137F0 Amzn.com/bill WA 15.00
Order Number 113-0000000-0000001
01/02 COFFEE SHOP ANYTOWN NC 20.00
JANE DOE Page2 of 3 Statement Date: 01/26/26
0000001 FIS33339 D 5 Y 9 26 26/01/26 Page 2 of 3 06654 MA MA 02336
"""

PAGE_3 = """\
ACCOUNT ACTIVITY (CONTINUED)
Date of
Transaction Merchant Name or Transaction Description $ Amount
01/10 HARDWARE STORE ANYTOWN NC 1,100.00
01/11 HARDWARE STORE ANYTOWN NC -1,000.00
INTEREST CHARGED
01/26 PURCHASE INTEREST CHARGE 2.50
TOTAL INTEREST FOR THIS PERIOD $2.50
2026 Totals Year-to-Date
Total fees charged in 2026 $0.00
PURCHASES
Purchases 22.74%(v)(d) - 0 - - 0 -
JANE DOE Page3 of 3 Statement Date: 01/26/26
"""

PAGES = [PAGE_1, PAGE_2, PAGE_3]


@pytest.fixture
def importer():
    return mod.Importer(account=ACCOUNT, account_number="1234")


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
    assert mod.parse_period(PAGE_1) == (date(2025, 12, 27), date(2026, 1, 26))
    assert mod.parse_summary(PAGE_1.splitlines()) == {
        "previous": Decimal("500.00"),
        "new": Decimal("137.50"),
    }


@pytest.mark.parametrize("mmdd, expected", [
    ("12/29", date(2025, 12, 29)),
    ("01/26", date(2026, 1, 26)),
    ("02/29", date(2024, 2, 29)),
])
def test_row_date_infers_year(mmdd, expected):
    end = date(2026, 1, 26) if mmdd != "02/29" else date(2024, 3, 26)
    assert mod.row_date(mmdd, end) == expected


def test_rows_are_negated_and_sorted(entries):
    got = [(t.date, t.postings[0].units) for t in transactions(entries)]
    assert got == [
        (date(2025, 12, 29), Amount(Decimal("100.00"), "USD")),
        (date(2025, 12, 30), Amount(Decimal("-15.00"), "USD")),
        (date(2026, 1, 2), Amount(Decimal("-20.00"), "USD")),
        (date(2026, 1, 10), Amount(Decimal("-1100.00"), "USD")),
        (date(2026, 1, 11), Amount(Decimal("1000.00"), "USD")),
        (date(2026, 1, 23), Amount(Decimal("400.00"), "USD")),
        (date(2026, 1, 26), Amount(Decimal("-2.50"), "USD")),
    ]
    assert all(t.postings[0].account == ACCOUNT for t in transactions(entries))


def test_order_number_is_joined_into_narration(entries):
    amazon = next(t for t in transactions(entries) if t.date == date(2025, 12, 30))
    assert amazon.payee == "AMAZON MKTPL*B563137F0 Amzn.com/bill WA"
    assert amazon.narration == (
        "AMAZON MKTPL*B563137F0 Amzn.com/bill WA Order Number 113-0000000-0000001"
    )


def test_description_is_kept_on_posting_through_decoration(importer, entries):
    importer.decorator = Decorator([Decoration({
        "re": "AMAZON", "narration": "Amazon order", "target_account": "Expenses:Shopping",
    })])
    importer.deduplicate(entries, [])
    amazon = next(t for t in transactions(entries) if t.date == date(2025, 12, 30))
    assert amazon.narration == "Amazon order"
    assert amazon.postings[0].meta["description"] == (
        "AMAZON MKTPL*B563137F0 Amzn.com/bill WA Order Number 113-0000000-0000001"
    )


def test_section_headings_and_footer_are_not_joined(entries):
    narrations = {t.narration for t in transactions(entries)}
    assert "AUTOMATIC PAYMENT - THANK YOU" in narrations
    assert "COFFEE SHOP ANYTOWN NC" in narrations
    assert "PURCHASE INTEREST CHARGE" in narrations


def test_new_balance_asserted_on_closing_date(entries):
    balances = [e for e in entries if isinstance(e, Balance)]
    assert len(balances) == 1
    assert balances[0].date == date(2026, 1, 26)
    assert balances[0].account == ACCOUNT
    assert balances[0].amount == Amount(Decimal("-137.50"), "USD")


def test_unreconciled_statement_is_rejected(importer, pages):
    pages[1] = pages[1].replace("20.00", "2.00")
    with pytest.raises(ValueError, match="balance changed"):
        importer.extract("statement.pdf", [])


@pytest.mark.parametrize("statement, configured, expected", [
    ("XXXX XXXX XXXX 1234", "1234", True),
    ("XXXX XXXX XXXX 1234", "4111 2222 3333 1234", True),
    ("4111 2222 3333 1234", "1234", True),
    ("XXXX XXXX XXXX 1234", "5678", False),
])
def test_identify_by_account_number(monkeypatch, pages, statement, configured, expected):
    monkeypatch.setattr(mod.mimetypes, "guess_type", lambda path: ("application/pdf", None))
    pages[0] = pages[0].replace("XXXX XXXX XXXX 1234", statement)
    assert mod.Importer(ACCOUNT, configured).identify("statement.pdf") is expected


def test_identify_rejects_other_banks(monkeypatch, importer, pages):
    monkeypatch.setattr(mod.mimetypes, "guess_type", lambda path: ("application/pdf", None))
    pages[0] = pages[0].replace("chase.com", "example.com")
    assert not importer.identify("statement.pdf")


def test_date_is_closing_date(importer, pages):
    assert importer.date("statement.pdf") == date(2026, 1, 26)


def ledger_txn(day, number, account=ACCOUNT):
    return Transaction(
        new_metadata("ledger.beancount", 1), day, "*",
        "Someone", "", frozenset(), frozenset(),
        [Posting(account, Amount(Decimal(number), "USD"), None, None, None, None)],
    )


def out_of_place(entries):
    return [e for e in transactions(entries) if "OUT_OF_PLACE" in e.tags]


def test_deduplicate_marks_existing_transactions(importer, entries):
    importer.deduplicate(entries, [ledger_txn(date(2026, 1, 3), "-20.00")])
    dupes = [e for e in transactions(entries) if DUPLICATE in e.meta]
    assert [d.date for d in dupes] == [date(2026, 1, 2)]


def test_extra_ledger_transaction_is_out_of_place(importer, entries):
    extra = ledger_txn(date(2026, 1, 26), "-55.00")
    importer.deduplicate(entries, [extra])
    oop = out_of_place(entries)
    assert [(e.date, e.postings[0].units.number) for e in oop] == [
        (date(2026, 1, 26), Decimal("-55.00")),
    ]
    assert oop[0].postings[0].flag == "!"


@pytest.mark.parametrize("day, account", [
    pytest.param(date(2025, 12, 26), ACCOUNT, id="before-period"),
    pytest.param(date(2026, 1, 27), ACCOUNT, id="after-period"),
    pytest.param(date(2026, 1, 5), "Liabilities:US:Chase:Other", id="other-account"),
])
def test_ledger_transactions_outside_statement_are_ignored(importer, entries, day, account):
    importer.deduplicate(entries, [ledger_txn(day, "-55.00", account)])
    assert out_of_place(entries) == []


def test_out_of_place_entries_are_not_decorated(importer, entries):
    importer.decorator = Decorator([Decoration({"re": ".", "target_account": "Expenses:Misc"})])
    importer.deduplicate(entries, [ledger_txn(date(2026, 1, 5), "-55.00")])
    (oop,) = out_of_place(entries)
    assert len(oop.postings) == 1
