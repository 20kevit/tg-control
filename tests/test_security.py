import unittest
from tg_control.security import Credentials


class CredentialTests(unittest.TestCase):
    def test_issue_rotate_revoke(self):
        store = Credentials()
        token = store.issue('fixture-client')
        self.assertEqual(store.authenticate(token), 'fixture-client')
        self.assertIsNone(store.authenticate(token + 'x'))
        self.assertNotIn(token, repr(store.records))
        replacement = store.rotate(token)
        self.assertIsNone(store.authenticate(token))
        self.assertEqual(store.authenticate(replacement), 'fixture-client')
        store.revoke(replacement)
        self.assertIsNone(store.authenticate(replacement))

    def test_unique_and_invalid(self):
        store = Credentials()
        self.assertNotEqual(store.issue('a'), store.issue('b'))
        self.assertIsNone(store.authenticate(None))
        self.assertIsNone(store.authenticate('x' * 257))
