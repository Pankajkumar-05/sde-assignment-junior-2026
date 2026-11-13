import pytest

from solution.normalize import (ist_date, ist_iso, normalize_header, normalize_phone, parse_date,
                                parse_decimal, parse_enum, parse_timestamp)


@pytest.mark.parametrize("raw", [
    "9876543210", "+91 98765 43210", "09876543210", "91-98765-43210",
    "(+91) 9876543210", "+919876543210", "  98765 43210  ", "919876543210"])
def test_phone_six_formats_all_canonicalise_the_same(raw):
    assert normalize_phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", [
    "987654321",            # 9 digits
    "98765432101",          # 11 digits not starting with 0
    "5876543210",           # bad prefix
    "022-2345-6789",        # landline
    "N/A", "", "   ",
    "+1 415 555 0134",
    "+91 5876543210",       # 12 digits, but remainder has bad prefix
    "0 5876543210",         # 11 digits with leading 0, remainder has bad prefix
    "9876543210 ext 5",     # extra digits
    "+91 98765 4321",       # 11 digits starting 91 -> not a valid shape
    "٩٨٧٦٥٤٣٢١٠",           # non-ASCII digits are not digits for this purpose
])
def test_phone_invalid(raw):
    assert normalize_phone(raw) is None


def test_phone_ten_digits_starting_91_keeps_its_digits():
    # The trap: a valid 10-digit number that merely starts with 91 must not be stripped.
    assert normalize_phone("9123456789") == "+919123456789"
    assert normalize_phone("+91 9123456789") == "+919123456789"
    assert normalize_phone("919123456789") == "+919123456789"


def test_phone_leading_zero_only_stripped_from_eleven_digits():
    assert normalize_phone("09123456789") == "+919123456789"
    assert normalize_phone("0912345678") is None  # 10 digits starting 0 is invalid


@pytest.mark.parametrize("first", "6789")
def test_phone_valid_prefixes(first):
    assert normalize_phone(f"{first}123456789") == f"+91{first}123456789"


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", "2026-12-01"),
    ("01/12/2026", "2026-12-01"),       # day-first
    ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-DEC-2026", "2026-12-01"),      # month name is case-insensitive
    ("01-dec-2026", "2026-12-01"),
    ("29/02/2028", "2028-02-29"),       # leap day
    (" 05/11/2026 ", "2026-11-05"),
])
def test_date_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "31/02/2026", "2026-13-01", "2026-02-30", "29/02/2027", "soon", "TBD", "", "12/31/2026",
    "01-Foo-2026", "2026/12/01", "1/2/26", "2026-12-01 10:00:00"])
def test_date_invalid(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("12345", "12345.00"), ("12345.5", "12345.50"), ("₹27,554", "27554.00"),
    ("₹ 1,23,456", "123456.00"), ("Rs. 5000", "5000.00"), ("Rs 5000", "5000.00"),
    ("rs.5000", "5000.00"), ("INR 25301.4", "25301.40"), ("inr25301.4", "25301.40"),
    ("0", "0.00"), ("10.005", "10.01"), ("10.004", "10.00"), ("  99.99 ", "99.99")])
def test_decimal_valid(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", [
    "N/A", "-", "twelve thousand", "-5", "₹-5", "NaN", "Infinity", "1e3", "", "Rs.", "12.3.4",
    "$100", "100 rupees"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("en", "en"), ("EN", "en"), (" hi ", "hi"), ("Ta", "ta")])
def test_enum_valid(raw, expected):
    assert parse_enum(raw, ["en", "hi", "ta"]) == expected


@pytest.mark.parametrize("raw", ["Hindi", "english", "xx", "e n"])
def test_enum_invalid(raw):
    assert parse_enum(raw, ["en", "hi", "ta"]) is None


@pytest.mark.parametrize("raw,expected", [
    ("customer_name", "customer name"), ("  Customer   Name ", "customer name"),
    ("Insured_Name", "insured name"), ("MOBILE__NO", "mobile no"),
    ("policy \t no", "policy no"), ("Phone", "phone"), ("Order  ID", "order id")])
def test_header_normalisation(raw, expected):
    assert normalize_header(raw) == expected


def test_timestamp_three_forms_agree():
    z = parse_timestamp("2026-10-03T04:31:00Z")
    off = parse_timestamp("2026-10-03T10:01:00+05:30")
    naive = parse_timestamp("2026-10-03 10:01:00")      # naive means IST
    assert z == off == naive


def test_timestamp_other_offsets_and_invalid():
    assert parse_timestamp("2026-10-03T10:01:00+00:00") == parse_timestamp("2026-10-03T10:01:00Z")
    assert parse_timestamp("not a time") is None
    assert parse_timestamp("") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp(12345) is None


def test_ist_date_crosses_midnight_relative_to_utc():
    # 20:00Z on the 3rd is 01:30 IST on the 4th
    us = parse_timestamp("2026-10-03T20:00:00Z")
    assert ist_date(us) == "2026-10-04"
    assert ist_iso(us) == "2026-10-04T01:30:00+05:30"
    # 18:29:59Z is still the 3rd in IST (23:59:59)
    assert ist_date(parse_timestamp("2026-10-03T18:29:59Z")) == "2026-10-03"
    assert ist_date(parse_timestamp("2026-10-03T18:30:00Z")) == "2026-10-04"


def test_timestamps_order_by_instant_not_by_text():
    a = parse_timestamp("2026-10-03T23:00:00Z")           # later instant, "smaller" text than b
    b = parse_timestamp("2026-10-04 01:00:00")            # naive IST = 2026-10-03T19:30Z
    assert a > b
