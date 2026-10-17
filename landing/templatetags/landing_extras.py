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


# ---- CRM display helpers (leads, inbox, bookings): Spanish, Puerto Rico time ----

_MESES = ('ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic')
_DIAS = ('lun', 'mar', 'mié', 'jue', 'vie', 'sáb', 'dom')


@register.filter(expects_localtime=True)
def fecha_es(value, style='full'):
    """A date/datetime in Spanish: 'short' = '9 mar', 'full' = '9 mar 2026',
    'dia' = 'lun 9 mar', 'hora' = '9 mar 2026 · 3:05 PM', 'diahora' = 'lun 9 mar · 3:05 PM'.
    Aware datetimes arrive already in the project time zone (expects_localtime)."""
    if not value:
        return ''
    try:
        base = f'{value.day} {_MESES[value.month - 1]}'
        dow = f'{_DIAS[value.weekday()]} '
    except AttributeError:
        return ''
    time = ''
    if hasattr(value, 'hour'):
        hour12 = value.hour % 12 or 12
        time = f' · {hour12}:{value.minute:02d} {"AM" if value.hour < 12 else "PM"}'
    if style == 'short':
        return base
    if style == 'dia':
        return dow + base
    if style == 'hora':
        return f'{base} {value.year}{time}'
    if style == 'diahora':
        return f'{dow}{base}{time}'
    return f'{base} {value.year}'


@register.filter
def ago(value):
    """'ahora', 'hace 5 min', 'hace 3 h', 'hace 2 d', 'hace 3 meses'."""
    if not value:
        return ''
    from django.utils import timezone
    seconds = int((timezone.now() - value).total_seconds())
    if seconds < 60:
        return 'ahora'
    minutes = seconds // 60
    if minutes < 60:
        return f'hace {minutes} min'
    hours = minutes // 60
    if hours < 24:
        return f'hace {hours} h'
    days = hours // 24
    if days < 60:
        return f'hace {days} d'
    months = days // 30
    return f'hace {months} meses'


@register.filter
def initials(user):
    """Up to two capital letters for an avatar chip: 'ana.perez@x.com' -> 'AP'."""
    if user is None:
        return ''
    import re
    name = (getattr(user, 'get_full_name', lambda: '')() or getattr(user, 'username', '') or '')
    parts = [p for p in re.split(r'[\s._@+-]+', name) if p]
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


@register.filter
def dict_get(mapping, key):
    """{{ labels|dict_get:key }} -> labels[key] or ''."""
    try:
        return mapping.get(key, '')
    except AttributeError:
        return ''
