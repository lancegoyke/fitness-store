import pytest

from store_project.payments.utils import int_to_price


def test_int_to_price():
    assert int_to_price(1000) == "10.00"
