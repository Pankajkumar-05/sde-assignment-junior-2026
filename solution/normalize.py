"""Pure value-normalisation helpers. No I/O, no database, no PII handling.

Every function returns ``None`` for "invalid", so callers decide what that means
(reject the row, or null the field and count a warning).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

IST = timezone(timedelta(hours=5, minutes=30))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_NON_DIGIT = re.compile(r"[^0-9]")  # ASCII digits only: no Unicode digit surprises
_HEADER_SEP = re.compile(r"[_\s]+")


def normalize_header(header: str) -> str:
    """lower-case, trim, then replace every run of '_' or whitespace with one space."""
    return _HEADER_SEP.sub(" ", header.strip().lower())


# --------------------------------------------------------------------- phone
def normalize_phone(raw: str) -> str | None:
    """Indian mobile -> '+91XXXXXXXXXX', or None if invalid.

    Strip non-digits; 12 digits starting '91' drops the '91'; 11 digits starting
    '0' drops the '0'. A 10-digit number that merely starts with 91 is untouched.
    """
    digits = _NON_DIGIT.sub("", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    return None


# ---------------------------------------------------------------------- date
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_ISO = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_DMY_SLASH = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})")
_DMY_DASH = re.compile(r"([0-9]{2})-([0-9]{2})-([0-9]{4})")
_DMON_Y = re.compile(r"([0-9]{2})-([A-Za-z]{3})-([0-9]{4})")


def parse_date(raw: str) -> str | None:
    """YYYY-MM-DD / DD/MM/YYYY / DD-MM-YYYY / DD-Mon-YYYY (day-first) -> 'YYYY-MM-DD'."""
    s = raw.strip()
    try:
        if m := _ISO.fullmatch(s):
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        elif m := (_DMY_SLASH.fullmatch(s) or _DMY_DASH.fullmatch(s)):
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        elif m := _DMON_Y.fullmatch(s):
            d, y = int(m[1]), int(m[3])
            mo = _MONTHS[m[2].lower()]
        else:
            return None
        return date(y, mo, d).isoformat()
    except (ValueError, KeyError):  # impossible date / unknown month name
        return None


# ------------------------------------------------------------------- decimal
_CURRENCY_PREFIX = re.compile(r"(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_TWO_PLACES = Decimal("0.01")


def parse_decimal(raw: str) -> str | None:
    """Optional currency prefix, comma thousands, >= 0 -> canonical 2-place string."""
    s = raw.strip()
    if m := _CURRENCY_PREFIX.match(s):
        s = s[m.end():]
    s = s.replace(",", "")
    if not _NUMBER.fullmatch(s):  # also rejects '-5', 'NaN', '1e3'
        return None
    try:
        return str(Decimal(s).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


# ---------------------------------------------------------------------- enum
def parse_enum(raw: str, values) -> str | None:
    v = raw.strip().lower()
    return v if v in {x.lower() for x in values} else None


# ----------------------------------------------------------------- timestamps
def parse_timestamp(raw) -> int | None:
    """Event timestamp -> UTC epoch microseconds. Naive means IST. None if unparseable."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)  # handles 'Z', '+05:30' and 'YYYY-MM-DD HH:MM:SS'
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return (dt - _EPOCH) // timedelta(microseconds=1)
    except (ValueError, OverflowError):
        return None


def _to_ist(us: int) -> datetime:
    return (_EPOCH + timedelta(microseconds=us)).astimezone(IST)


def ist_date(us: int) -> str:
    return _to_ist(us).date().isoformat()


def ist_iso(us: int) -> str:
    """e.g. 2026-10-14T17:55:24+05:30"""
    return _to_ist(us).isoformat(timespec="seconds")
