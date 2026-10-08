"""AI usage accounting: what each Anthropic call costs.

Prices live in settings.DOMINIO_MODEL_PRICES (USD per million tokens, matched
by model-id prefix). A model missing from the table falls back to the blended
DOMINIO_COST_PER_MTOK rate on all tokens, and logs a warning so the table gets
updated.
"""
import logging
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from . import agent
from .models import UsageEvent

logger = logging.getLogger(__name__)

_MILLION = Decimal(1_000_000)
_MICRO = Decimal('0.000001')

# agent usage-dict key -> UsageEvent column.
_TOKEN_FIELDS = (
    ('input_tokens', 'input_tokens'),
    ('output_tokens', 'output_tokens'),
    ('cache_read_input_tokens', 'cache_read_tokens'),
    ('cache_creation_input_tokens', 'cache_write_tokens'),
)


def _prices_for(model):
    table = getattr(settings, 'DOMINIO_MODEL_PRICES', {}) or {}
    matches = [prefix for prefix in table if model and model.startswith(prefix)]
    return table[max(matches, key=len)] if matches else None


def cost_for(model, usage):
    """USD cost (Decimal) of a usage dict from landing.agent.new_usage()."""
    def tok(key):
        return Decimal(usage.get(key, 0) or 0)

    prices = _prices_for(model)
    if prices is None:
        logger.warning('No price for model %r; using DOMINIO_COST_PER_MTOK', model)
        blended = Decimal(str(getattr(settings, 'DOMINIO_COST_PER_MTOK', 0.10)))
        total = sum(tok(k) for k in ('input_tokens', 'output_tokens',
                                      'cache_read_input_tokens', 'cache_creation_input_tokens'))
        return (total * blended / _MILLION).quantize(Decimal('0.000001'))

    def rate(key):
        return Decimal(str(prices.get(key, 0)))

    cost = (tok('input_tokens') * rate('input')
            + tok('output_tokens') * rate('output')
            + tok('cache_read_input_tokens') * rate('cache_read')
            + tok('cache_creation_input_tokens') * rate('cache_write')) / _MILLION
    return cost.quantize(Decimal('0.000001'))


def record(kind, usage, client=None, conversation=None):
    """Write one UsageEvent for a finished Anthropic call. Best-effort: the
    reply has already been produced, so a ledger failure is logged and never
    raised. Call it AFTER (outside) any business transaction. A usage with no
    tokens (nothing was billed, or a mock) writes nothing. Returns the event or
    None."""
    try:
        if not usage:
            return None
        tokens = {field: int(usage.get(key, 0) or 0) for key, field in _TOKEN_FIELDS}
        if not any(tokens.values()):
            return None
        model = usage.get('model') or agent._model()
        # Own savepoint: a failed insert must not poison a caller's transaction.
        with transaction.atomic():
            return UsageEvent.objects.create(
                kind=kind,
                client=client,
                client_slug=(client.slug if client is not None else '')[:60],
                conversation=conversation,
                model=model[:60],
                api_calls=min(int(usage.get('api_calls') or 1), 32767),
                effort=str(usage.get('effort') or '')[:10],
                cost_usd=cost_for(model, usage),
                **tokens,
            )
    except Exception:
        logger.exception('Failed to record %s usage', kind)
        return None


def month_bounds(date=None):
    """(start, end) aware datetimes of the calendar month containing `date`
    (default: today) in settings.TIME_ZONE. `end` is exclusive."""
    tz = timezone.get_default_timezone()
    if date is None:
        day = timezone.localdate(timezone=tz)
    elif isinstance(date, datetime):
        day = timezone.localtime(date, tz).date() if timezone.is_aware(date) else date.date()
    else:
        day = date
    start = timezone.make_aware(datetime(day.year, day.month, 1), tz)
    nxt_year, nxt_month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return start, timezone.make_aware(datetime(nxt_year, nxt_month, 1), tz)


def rollup_by_client(start, end, client_ids=None):
    """Usage per client in [start, end), in one grouped query.

    Returns {client_id: {'events', 'messages', 'input', 'output', 'cache_read',
    'cache_write', 'tokens', 'cost'}}. 'messages' counts chat turns only; cost
    is a Decimal. The None key holds events with no client (the public demo,
    deleted tenants). `client_ids` restricts to those clients (None = all)."""
    qs = UsageEvent.objects.filter(created_at__gte=start, created_at__lt=end)
    if client_ids is not None:
        qs = qs.filter(client_id__in=list(client_ids))
    rows = (
        qs.order_by()
        .values('client_id')
        .annotate(
            events=Count('id'),
            messages=Count('id', filter=Q(kind='chat')),
            input=Sum('input_tokens'),
            output=Sum('output_tokens'),
            cache_read=Sum('cache_read_tokens'),
            cache_write=Sum('cache_write_tokens'),
            cost=Sum('cost_usd'),
        )
    )
    result = {}
    for row in rows:
        entry = {k: row[k] or 0 for k in
                 ('events', 'messages', 'input', 'output', 'cache_read', 'cache_write')}
        entry['tokens'] = (entry['input'] + entry['output']
                           + entry['cache_read'] + entry['cache_write'])
        entry['cost'] = Decimal(row['cost'] or 0).quantize(_MICRO)
        result[row['client_id']] = entry
    return result


def monthly(client_ids=None, months=12):
    """Per-month, per-client usage for the last `months` calendar months
    (current one included), oldest first, in one query. Rows are
    {'month' (aware datetime, first of month), 'client_id', 'messages',
    'tokens', 'cost'}."""
    start, _end = month_bounds()
    year, month = start.year, start.month - (max(1, months) - 1)
    while month <= 0:
        month += 12
        year -= 1
    since = timezone.make_aware(datetime(year, month, 1), timezone.get_default_timezone())

    qs = UsageEvent.objects.filter(created_at__gte=since)
    if client_ids is not None:
        qs = qs.filter(client_id__in=list(client_ids))
    rows = (
        qs.order_by()
        .annotate(month=TruncMonth('created_at'))
        .values('month', 'client_id')
        .annotate(
            messages=Count('id', filter=Q(kind='chat')),
            tokens=Sum(F('input_tokens') + F('output_tokens')
                       + F('cache_read_tokens') + F('cache_write_tokens')),
            cost=Sum('cost_usd'),
        )
        .order_by('month', 'client_id')
    )
    return [{'month': r['month'], 'client_id': r['client_id'],
             'messages': r['messages'] or 0, 'tokens': r['tokens'] or 0,
             'cost': Decimal(r['cost'] or 0).quantize(_MICRO)} for r in rows]
