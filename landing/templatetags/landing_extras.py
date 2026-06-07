import re

from django import template

register = template.Library()


@register.filter
def format_phone(value):
    """Format a US/PR number as (XXX) XXX-XXXX. Returns the input unchanged if it
    isn't a 10-digit (or 1+10) number, so international numbers still show."""
    if not value:
        return ''
    digits = re.sub(r'\D', '', str(value))
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]
    if len(digits) == 10:
        return f'({digits[0:3]}) {digits[3:6]}-{digits[6:]}'
    return value
