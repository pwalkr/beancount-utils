import unittest
import tempfile
import os
import yaml
from unittest.mock import patch
from collections import namedtuple

from beancount.core.data import Posting, Transaction

from beancount_utils.decorator import Decorator, Decoration

class TestDecorator(unittest.TestCase):
    def setUp(self):
        self.tx = Transaction(
            meta=None, date=None, links=None,
            payee="Foo Bar", flag="!", narration="Old", tags=set(['old']),
            postings=[Posting('Assets:Cash', 100, None, None, None, None)]
        )
        # Decoration that matches "Foo Bar" and sets flag
        self.decoration = Decoration({'re': 'foo', 'flag': '*', 'narration': 'New', 'payee': 'Fizz Buzz', 'tags': {'new'}})
        self.decorator = Decorator([self.decoration])

    def test_decorate_transaction_applies_decoration(self):
        tx2 = self.decorator.decorate_transaction(self.tx)
        self.assertEqual(tx2.flag, '*')
        self.assertEqual(tx2.narration, 'New')
        self.assertEqual(tx2.payee, 'Fizz Buzz')
        self.assertIn('new', tx2.tags)
        self.assertIn('old', tx2.tags)

    def test_decorate_updates_entries_in_place(self):
        entries = [self.tx]
        self.decorator.decorate(entries)
        self.assertEqual(entries[0].flag, '*')
        self.assertEqual(entries[0].narration, 'New')
        self.assertEqual(entries[0].payee, 'Fizz Buzz')
        self.assertIn('new', entries[0].tags)
        self.assertIn('old', entries[0].tags)

    def test_decorate_skips_non_transaction(self):
        entries = ["not a transaction", self.tx]
        # Should not raise an error
        self.decorator.decorate(entries)
        self.assertEqual(entries[0], "not a transaction")
        self.assertEqual(entries[1].flag, '*')

    def test_decorate_respects_exclude(self):
        decorator = Decorator([self.decoration], exclude=lambda x: True)
        entries = [self.tx]
        decorator.decorate(entries)
        # Should not be decorated
        self.assertEqual(entries[0].flag, '!')

    def test_first_matching_decoration_takes_precedence(self):
        # Two decorations, both match, but the first should be applied
        dec1 = Decoration({'re': 'foo', 'flag': '*', 'narration': 'First'})
        dec2 = Decoration({'re': 'foo', 'flag': '!', 'narration': 'Second'})
        decorator = Decorator([dec1, dec2])
        tx = self.tx  # from setUp
        tx2 = decorator.decorate_transaction(tx)
        self.assertEqual(tx2.flag, '*')
        self.assertEqual(tx2.narration, 'First')


class TestDecoration(unittest.TestCase):
    def setUp(self):
        self.tx = Transaction(
            meta=None, date=None, links=None,
            payee="Foo Bar", flag="!", narration="Old", tags=set(['old']), postings=[Posting('Assets:Cash', 100, None, None, None, None)]
        )

    def test_match_true(self):
        # Case insensitiev
        d = Decoration({'re': 'fOo'})
        self.assertTrue(d.match(self.tx))

    def test_match_false_no_payee(self):
        d = Decoration({'re': 'foo'})
        tx = Transaction(
            meta=None, date=None, links=None,
            payee=None, flag="*", narration="Old", tags=set(), postings=[]
        )
        self.assertFalse(d.match(tx))

    def test_match_false_no_match(self):
        d = Decoration({'re': 'baz'})
        self.assertFalse(d.match(self.tx))

    def test_decorate_updates_fields(self):
        d = Decoration({'re': 'foo', 'flag': '*', 'narration': 'New', 'payee': 'Bar', 'tags': {'new'}})
        # Transaction is a namedtuple, so _replace returns a new instance
        tx2 = d.decorate(self.tx)
        self.assertEqual(tx2.flag, '*')
        self.assertEqual(tx2.narration, 'New')
        self.assertEqual(tx2.payee, 'Bar')
        self.assertIn('new', tx2.tags)
        self.assertIn('old', tx2.tags)

    def test_decorate_adds_posting(self):
        d = Decoration({'re': 'foo', 'target_account': 'Expenses:Test'})
        tx2 = d.decorate(self.tx)
        self.assertEqual(tx2.postings[-1].account, 'Expenses:Test')
        self.assertEqual(tx2.postings[-1].units, -self.tx.postings[0].units)


class TestDecoratorFromYaml(unittest.TestCase):
    def setUp(self):
        # Example decorations
        self.decoration1 = {'re': 'test1', 'flag': '*'}
        self.decoration2 = {'re': 'test2', 'flag': '!'}
        # Create temp YAML files
        self.temp_files = []
        for decoration in [self.decoration1, self.decoration2]:
            fd, path = tempfile.mkstemp(suffix='.yaml')
            with os.fdopen(fd, 'w') as f:
                yaml.dump([decoration], f)
            self.temp_files.append(path)

    def tearDown(self):
        for path in self.temp_files:
            os.remove(path)

    @patch('beancount_utils.decorator.load_yaml')
    @patch.object(Decorator, 'from_list')
    def test_from_yaml_single_file(self, mock_from_list, mock_load_yaml):
        mock_load_yaml.return_value = [self.decoration1]
        Decorator.from_yaml(self.temp_files[0])
        mock_load_yaml.assert_called_once_with(self.temp_files[0])
        mock_from_list.assert_called_once_with([self.decoration1])

if __name__ == '__main__':
    unittest.main()