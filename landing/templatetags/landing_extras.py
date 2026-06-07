from django import template

from ..phone import format_phone as _format_phone

register = template.Library()


@register.filter
def format_phone(value):
    """Display a US/PR number as (XXX) XXX-XXXX (single source of truth in
    landing.phone). Returns the input unchanged if it isn't a 10-digit number."""
    return _format_phone(value)
