from django.test import TestCase

from google.oauth2.credentials import Credentials

from spreadsheet import get_creds


class SpreadsheetTestCase(TestCase):
    def setUp(self) -> None:
        self.creds = get_creds()

    def test_get_creds(self) -> None:
        self.assertTrue(type(self.creds), Credentials)
