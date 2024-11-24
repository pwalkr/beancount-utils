import unittest
import textwrap
from unittest.mock import patch
from beancount_utils.parse.PdfUtilityStatement import PdfUtilityStatement, extract_charges

class TestPdfUtilityStatement(unittest.TestCase):

    def setUp(self):
        self.mock_pdf_text = textwrap.dedent("""
            ...
            Account Number
            Due Date
            Bank Draft Day
            Amount Due
            14380-3548-321
            1/23/2015
            1/21/2015
            $123.45
            ...
            GAL
            Current Billing
            Water Base Rate
            23.45
            Water Usage Rate
            54.32
            Sewer Base Rate
            12.34
            Sewer Usage Rate
            43.21
            Gar/Recycle Fee
            33.00
            Charge Code
            Amount
            Current Charges
            166.32
            Balance Due
            170.00
            ...
        """)


    def test_extract_charges(self):
        charges = extract_charges(self.mock_pdf_text)
        self.assertEqual(charges['Sewer Base Rate'], 12.34)
        charges = coalesce_charges(charges)
        self.assertEqual(charges['sewer'], 55.55)


if __name__ == '__main__':
    unittest.main()

