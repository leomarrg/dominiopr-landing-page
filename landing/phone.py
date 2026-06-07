"""Single source of truth for phone validation, normalization and display.

Market is Puerto Rico / US (NANP), so a valid number is 10 digits (optionally a
leading 1). No heavy `phonenumbers` dependency — the box is small and the rule is
simple. Stored normalized as E.164 (+1XXXXXXXXXX); shown as (XXX) XXX-XXXX.
"""
import re


def _digits(value):
    d = re.sub(r'\D', '', str(value or ''))
    if len(d) == 11 and d[0] == '1':
        d = d[1:]
    return d


def normalize_phone(value):
    """'' for blank (phone is optional), '+1XXXXXXXXXX' for a 10-digit US/PR
    number, or None when the input is present but not a real number (letters,
    wrong length). Kept simple on purpose — rejects garbage, accepts real numbers."""
    if not str(value or '').strip():
        return ''
    d = _digits(value)
    return '+1' + d if len(d) == 10 else None


def format_phone(value):
    """Display helper: (XXX) XXX-XXXX, or the input unchanged if not 10 digits."""
    if not value:
        return ''
    d = _digits(value)
    if len(d) == 10:
        return '(%s) %s-%s' % (d[0:3], d[3:6], d[6:])
    return value
