import re
import datetime
from datetime import datetime as dt
from pypdf import PdfReader


def from_pdf(file):
    text = ''.join([ page.extract_text() for page in PdfReader(file).pages ])
    return from_text(text)


def from_text(text):
    info = cls.extract_info(text)
    charges = coalesce_charges(extract_charges(text))
    return PdfUtilityStatement(
        account_number=info['account_number'],
        amount_due=info['amount_due'],
        charge=charges,
        draft_day=info['draft_day'])


def extract_charges(pdf_text):
    match = re.search(r'Current Billing\n(.*)Charge Code\n', pdf_text)
    if not match:
        raise Exception("No charges found")
    else:
        charges = match.group(1).splitlines()
        if charges%2 != 0:
            raise Exception("Uneven charges: {}".format(charges))
        return { charges[x].strip(): float(charges[x+1].strip()) for x in range(0, len(charges), 2) }


def extract_info(text):
    match = re.search(r'Account Number\nDue Date\nBank Draft Day\nAmount Due\n([0-9-/$]+\n){4}', pdf_text)
    if not match:
        raise Exception("No info found")
    else:
        info = match.group(1).splitlines()
        return {
            account_number: info[0].strip(),
            amount_due: info[3].strip(),
            draft_day: dt.strptime(info[2].strip(), '%m/%d/%Y'),
            due_date: dt.strptime(info[1].strip(), '%m/%d/%Y'),
        }


def coalesce_charges(raw):
    charges = {}
    for key, value in raw.items():
        if re.search('water', key, re.IGNORECASE):
            charges['water'] = charges.get('water', 0) + value
        elif re.search('sewer', key, re.IGNORECASE):
            charges['sewer'] = charges.get('sewer', 0) + value
        elif re.search('gar/rec', key, re.IGNORECASE):
            charges['trash'] = charges.get('trash', 0) + value
        else:
            charges[key] = value


class PdfUtilityStatement:
    account_number: str
    amount_due: float
    charges: list
    draft_day: datetime.date
