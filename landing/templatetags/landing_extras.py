from django import template

from ..phone import format_phone as _format_phone

register = template.Library()


@register.filter
def format_phone(value):
    """Display a US/PR number as (XXX) XXX-XXXX (single source of truth in
    landing.phone). Returns the input unchanged if it isn't a 10-digit number."""
    return _format_phone(value)


@register.filter
def usd(value, places=2):
    """Money for the control center: '$1,234.50', '-$12.00', '—' for None
    (unknown price). Keeps the minus sign before the dollar sign."""
    if value is None or value == '':
        return '—'
    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(str(value))
        places = int(places)
    except (InvalidOperation, TypeError, ValueError):
        return '—'
    text = f'{abs(amount):,.{places}f}'
    negative = amount < 0 and any(ch not in '0.,' for ch in text)
    return f'-${text}' if negative else f'${text}'


@register.filter
def thousands(value):
    """12345 -> '12,345' (no humanize app just for this)."""
    try:
        return f'{int(value):,}'
    except (TypeError, ValueError):
        return value
