"""Platform analytics: volume, intents, revenue and config per tenant.

Shared by the staff control center (landing.control_views) and, next, the
tenant dashboard. Two rules keep it safe to run with hundreds of clients:

  * every function issues a FIXED number of grouped queries, never one per
    client; results are merged in Python keyed by client_id;
  * `client_ids=None` means "all clients" and is only for staff callers. A
    tenant caller must always pass its own member client ids.

Only portable ORM constructs are used (SQLite now, Postgres later).
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import Length, TruncDate, TruncMonth
from django.utils import timezone

from . import entitlements
from . import usage as usage_ledger
from .models import (
    AvailabilityRule, Booking, ChatMessage, ContactSubmission, Conversation,
    KnowledgeSource, Membership, Survey, UsageEvent,
)

_CENT = Decimal('0.01')
_MICRO = Decimal('0.000001')

# Subscription statuses that count as recurring revenue. past_due is still a
# customer we expect to collect from (Stripe is retrying); incomplete never
# paid and canceled is gone, so both count as $0.
REVENUE_STATUSES = ('active', 'past_due')

INTENT_LABELS = dict(Conversation.INTENT_CHOICES)

PERIODS = [
    ('month', 'Este mes'),
    ('last_month', 'Mes pasado'),
    ('30d', 'Últimos 30 días'),
    ('90d', 'Últimos 90 días'),
]
PERIOD_LABELS = dict(PERIODS)


def period_bounds(key):
    """(key, start, end) for a period selector value; unknown -> 'month'.
    `end` is exclusive."""
    now = timezone.now()
    if key == 'last_month':
        this_start, _ = usage_ledger.month_bounds()
        start, end = usage_ledger.month_bounds(this_start - timedelta(days=1))
        return key, start, end
    if key == '30d':
        return key, now - timedelta(days=30), now
    if key == '90d':
        return key, now - timedelta(days=90), now
    start, end = usage_ledger.month_bounds()
    return 'month', start, end


def _scoped(qs, client_ids, field='client_id'):
    if client_ids is None:
        return qs
    return qs.filter(**{f'{field}__in': list(client_ids)})


# ------------------------------------------------------------------
# Revenue
# ------------------------------------------------------------------

def plan_monthly_revenue(subscription):
    """Monthly recurring revenue (Decimal, 2 places) of one subscription.

    * monthly_price_cents, when set, wins (custom plans, negotiated discounts);
    * otherwise the PLANS table: monthly price, or annual / 12;
    * None = unknown price (custom plan with no override) -- shown as "—";
    * Decimal('0.00') when the status is not in REVENUE_STATUSES or there is
      no subscription at all: no recurring revenue, but AI cost still counts
      against the margin.
    """
    if subscription is None or subscription.status not in REVENUE_STATUSES:
        return Decimal('0.00')
    if subscription.monthly_price_cents is not None:
        return (Decimal(subscription.monthly_price_cents) / 100).quantize(_CENT)
    from .views import plan_pricing  # late import: views is heavy and may import us later
    plan = plan_pricing(subscription.plan, subscription.step) or {}
    if subscription.period == 'annual' and plan.get('annual') is not None:
        return (Decimal(plan['annual']) / 12).quantize(_CENT)
    if plan.get('monthly') is None:
        return None
    return Decimal(plan['monthly']).quantize(_CENT)


# ------------------------------------------------------------------
# Volume
# ------------------------------------------------------------------

def _empty_volume():
    return {
        'conversations': 0, 'escalated': 0, 'leads': 0, 'chat_leads': 0,
        'bookings': 0, 'messages': 0, 'events': 0,
        'input': 0, 'output': 0, 'cache_read': 0, 'cache_write': 0, 'tokens': 0,
        'cost': Decimal('0').quantize(_MICRO), 'csat_avg': None, 'csat_n': 0,
    }


def volume(client_ids, start, end):
    """Per-client activity in [start, end). Five grouped queries in total.

    Returns {client_id: {'conversations', 'escalated', 'leads', 'chat_leads',
    'bookings', 'messages', 'events', 'input', 'output', 'cache_read',
    'cache_write', 'tokens', 'cost', 'csat_avg', 'csat_n'}}. 'messages' and
    the token/cost figures come from the usage ledger. The None key collects
    rows with no client (public demo usage, legacy leads/bookings)."""
    result = {}

    def row_for(cid):
        if cid not in result:
            result[cid] = _empty_volume()
        return result[cid]

    convs = (_scoped(Conversation.objects.filter(started_at__gte=start, started_at__lt=end),
                     client_ids)
             .order_by().values('client_id')
             .annotate(n=Count('id'), esc=Count('id', filter=Q(state='escalated'))))
    for r in convs:
        row = row_for(r['client_id'])
        row['conversations'], row['escalated'] = r['n'], r['esc']

    leads = (_scoped(ContactSubmission.objects.filter(created_at__gte=start, created_at__lt=end),
                     client_ids)
             .order_by().values('client_id')
             .annotate(n=Count('id'), chat=Count('id', filter=Q(source='chat'))))
    for r in leads:
        row = row_for(r['client_id'])
        row['leads'], row['chat_leads'] = r['n'], r['chat']

    bookings = (_scoped(Booking.objects.filter(created_at__gte=start, created_at__lt=end),
                        client_ids)
                .order_by().values('client_id').annotate(n=Count('id')))
    for r in bookings:
        row_for(r['client_id'])['bookings'] = r['n']

    csat = (_scoped(Survey.objects.filter(created_at__gte=start, created_at__lt=end),
                    client_ids, field='conversation__client_id')
            .order_by().values('conversation__client_id')
            .annotate(avg=Avg('score'), n=Count('id')))
    for r in csat:
        row = row_for(r['conversation__client_id'])
        row['csat_avg'] = round(r['avg'], 1) if r['avg'] is not None else None
        row['csat_n'] = r['n']

    for cid, used in usage_ledger.rollup_by_client(start, end, client_ids).items():
        row = row_for(cid)
        for key in ('events', 'messages', 'input', 'output', 'cache_read',
                    'cache_write', 'tokens', 'cost'):
            row[key] = used[key]
    return result


def empty_volume():
    """A zeroed volume row, for clients with no activity in the period."""
    return _empty_volume()


def local_months(months):
    """First-of-month aware datetimes for the last `months` calendar months,
    oldest first (current month included)."""
    tz = timezone.get_default_timezone()
    start, _ = usage_ledger.month_bounds()
    year, month = start.year, start.month
    out = []
    for _ in range(max(1, months)):
        out.append(timezone.make_aware(datetime(year, month, 1), tz))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(out))


def daily_series(client_id, days=30):
    """Last `days` local days for one client, oldest first, zero-filled. Two
    grouped queries. Each item: {'day', 'messages', 'cost', 'conversations'}."""
    tz = timezone.get_default_timezone()
    today = timezone.localdate()
    first = today - timedelta(days=days - 1)
    since = timezone.make_aware(datetime(first.year, first.month, first.day), tz)

    used = {
        r['day']: r for r in
        UsageEvent.objects.filter(client_id=client_id, created_at__gte=since)
        .order_by().annotate(day=TruncDate('created_at', tzinfo=tz)).values('day')
        .annotate(messages=Count('id', filter=Q(kind='chat')), cost=Sum('cost_usd'))
    }
    convs = dict(
        Conversation.objects.filter(client_id=client_id, started_at__gte=since)
        .order_by().annotate(day=TruncDate('started_at', tzinfo=tz)).values('day')
        .annotate(n=Count('id')).values_list('day', 'n'))
    series = []
    for i in range(days):
        day = first + timedelta(days=i)
        u = used.get(day, {})
        series.append({
            'day': day,
            'messages': u.get('messages') or 0,
            'cost': Decimal(u.get('cost') or 0).quantize(_MICRO),
            'conversations': convs.get(day, 0),
        })
    return series


def svg_bars(series, key, width=600, height=120):
    """Bar geometry for an inline SVG (no chart library; CSP is script-src 'self')."""
    if not series:
        return []
    peak = max(d[key] for d in series) or 1
    step = width / len(series)
    bars = []
    for i, d in enumerate(series):
        h = (d[key] / peak) * (height - 4) if d[key] else 0
        bars.append({
            'x': f'{i * step + 1:.1f}', 'w': f'{max(step - 2, 1):.1f}',
            'y': f'{height - h:.1f}', 'h': f'{h:.1f}',
            'day': d['day'], 'value': d[key],
        })
    return bars


def hbars(items, key='n'):
    """Adds 'pct' (relative to the largest item) for CSS horizontal bars."""
    peak = max((i[key] for i in items), default=0) or 1
    for i in items:
        i['pct'] = round(i[key] * 100 / peak)
    return items


def monthly_table(client_id, months=12):
    """Per-month usage for one client, split by UsageEvent kind, oldest first,
    zero-filled. One grouped query. Each item: {'month', 'kinds': {kind:
    {'events', 'tokens', 'cost'}}, 'messages', 'input', 'output',
    'cache_read', 'cache_write', 'tokens', 'cost'}."""
    month_starts = local_months(months)
    rows = (
        UsageEvent.objects.filter(client_id=client_id, created_at__gte=month_starts[0])
        .order_by().annotate(month=TruncMonth('created_at')).values('month', 'kind')
        .annotate(events=Count('id'), input=Sum('input_tokens'), output=Sum('output_tokens'),
                  cache_read=Sum('cache_read_tokens'), cache_write=Sum('cache_write_tokens'),
                  cost=Sum('cost_usd'))
    )
    table = {}
    for m in month_starts:
        table[(m.year, m.month)] = {
            'month': m, 'kinds': {}, 'messages': 0, 'input': 0, 'output': 0,
            'cache_read': 0, 'cache_write': 0, 'tokens': 0, 'cost': Decimal('0').quantize(_MICRO)}
    for r in rows:
        local = timezone.localtime(r['month']) if timezone.is_aware(r['month']) else r['month']
        entry = table.get((local.year, local.month))
        if entry is None:
            continue
        tok = {k: r[k] or 0 for k in ('input', 'output', 'cache_read', 'cache_write')}
        tokens = sum(tok.values())
        cost = Decimal(r['cost'] or 0).quantize(_MICRO)
        entry['kinds'][r['kind']] = {'events': r['events'], 'tokens': tokens, 'cost': cost}
        if r['kind'] == 'chat':
            entry['messages'] = r['events']
        for k, v in tok.items():
            entry[k] += v
        entry['tokens'] += tokens
        entry['cost'] += cost
    return [table[(m.year, m.month)] for m in month_starts]


# ------------------------------------------------------------------
# What visitors ask
# ------------------------------------------------------------------

def top_intents(client_ids, start, end):
    """[{'intent', 'label', 'n'}] for tagged conversations started in the
    period, most frequent first. One query."""
    rows = (_scoped(Conversation.objects.filter(started_at__gte=start, started_at__lt=end,
                                                tagged_at__isnull=False),
                    client_ids)
            .exclude(intent='').order_by().values('intent')
            .annotate(n=Count('id')).order_by('-n', 'intent'))
    return [{'intent': r['intent'], 'label': INTENT_LABELS.get(r['intent'], r['intent']),
             'n': r['n']} for r in rows]


def top_topics(client_ids, start, end, n=10):
    """[{'topic', 'n'}], most frequent first. Clinic clients never have a
    topic, so they contribute nothing. One query."""
    rows = (_scoped(Conversation.objects.filter(started_at__gte=start, started_at__lt=end),
                    client_ids)
            .exclude(topic='').order_by().values('topic')
            .annotate(n=Count('id')).order_by('-n', 'topic')[:n])
    return list(rows)


def recent_first_questions(client_id, n=20):
    """Latest opening questions of one client. Always empty for clinic_mode
    clients (enforced in the query, not only by the template). One query."""
    return list(
        Conversation.objects.filter(client_id=client_id, client__clinic_mode=False)
        .exclude(first_question='').order_by('-started_at')
        .values('pk', 'first_question', 'intent', 'started_at')[:n])


# ------------------------------------------------------------------
# Config / permissions
# ------------------------------------------------------------------

def knowledge_stats(client_ids=None):
    """{client_id: {'active', 'draft', 'chars'}}; chars = active content length
    (URL sources included once processed). One query."""
    rows = (_scoped(KnowledgeSource.objects.exclude(status='archived'), client_ids)
            .order_by().values('client_id')
            .annotate(active=Count('id', filter=Q(status='active')),
                      draft=Count('id', filter=Q(status='draft')),
                      chars=Sum(Length('content'), filter=Q(status='active'))))
    return {r['client_id']: {'active': r['active'], 'draft': r['draft'],
                             'chars': r['chars'] or 0} for r in rows}


def member_roles(client_ids=None):
    """{client_id: {'org_admin': n, 'agent': n}}. One query."""
    out = {}
    rows = (_scoped(Membership.objects.all(), client_ids)
            .order_by().values('client_id', 'role').annotate(n=Count('id')))
    for r in rows:
        out.setdefault(r['client_id'], {'org_admin': 0, 'agent': 0})[r['role']] = r['n']
    return out


def today_cap_key(slug):
    # Must match the per-tenant counter in views.chat_api (UTC date there).
    return f'chat:client:{slug}:' + timezone.now().strftime('%Y%m%d')


def today_cap_usage(clients):
    """{client_id: {'used', 'cap', 'pct'}} from today's chat counters, in ONE
    cache.get_many. Call it with the clients actually displayed (one page),
    not every client. pct is None when the client has no cap."""
    clients = list(clients)
    keys = {today_cap_key(c.slug): c for c in clients}
    try:
        values = cache.get_many(list(keys)) if keys else {}
    except Exception:  # a cache hiccup must not break the dashboard
        values = {}
    out = {}
    for key, c in keys.items():
        used = int(values.get(key) or 0)
        cap = c.daily_message_cap or 0
        out[c.pk] = {'used': used, 'cap': cap,
                     'pct': min(100, round(used * 100 / cap)) if cap else None}
    return out


# ------------------------------------------------------------------
# Tenant dashboard: allowance, business hours, value for money
# ------------------------------------------------------------------
# Everything below is safe to hand to a TENANT: no function returns a DOMINIO
# cost figure (cost_usd). Prices here are what the client PAYS us.

BUSINESS_TZ = ZoneInfo('America/Puerto_Rico')
# Used for a client with no AvailabilityRule rows: Mon-Fri 8:00-17:00.
DEFAULT_HOURS = {wd: (time(8, 0), time(17, 0)) for wd in range(5)}
ALLOWANCE_WARN_PCT = 80


def _subscription_of(client):
    # A missing reverse one-to-one raises RelatedObjectDoesNotExist, an
    # AttributeError subclass, so getattr's default covers "no subscription".
    return getattr(client, 'subscription', None)


def _local_days(start, end):
    """Number of local calendar days in [start, end)."""
    return max(1, (timezone.localtime(end, BUSINESS_TZ).date()
                   - timezone.localtime(start, BUSINESS_TZ).date()).days)


def included_messages(client, days):
    """Messages the client's plan includes for a month of `days` days: the
    plan's monthly_messages when PLAN_LIMITS defines one, otherwise the
    per-tenant daily cap times the days in the month."""
    from .views import PLAN_LIMITS  # late import, same reason as plan_monthly_revenue
    sub = _subscription_of(client)
    monthly = (PLAN_LIMITS.get(sub.plan) or {}).get('monthly_messages') if sub else None
    if monthly is not None:
        return int(monthly)
    return int(client.daily_message_cap or 0) * days


def allowance(client, month_start, month_end):
    """The client's message allowance for one calendar month. One grouped
    query (usage ledger) plus one cache.get_many for today's counter.

    Returns {'used', 'included', 'pct', 'bar_pct', 'warn', 'projection',
    'projection_pct', 'projection_warn', 'days', 'elapsed', 'today',
    'today_cap', 'today_pct', 'today_warn', 'tokens'}. pct/projection_pct are
    None when nothing is included (cap 0). 'tokens' is the month's total
    tokens, for the technical detail; no cost is ever returned."""
    used_row = usage_ledger.rollup_by_client(month_start, month_end, [client.pk]).get(client.pk, {})
    days = _local_days(month_start, month_end)
    first_day = timezone.localtime(month_start, BUSINESS_TZ).date()
    today = timezone.localtime(timezone.now(), BUSINESS_TZ).date()
    elapsed = min(days, max(0, (today - first_day).days + 1))
    # Phase C: packages are measured in conversations per month (with
    # overage billing); the launch plans keep the daily-cap-in-messages view.
    status = entitlements.allowance_status(client, month_start)
    by_conversations = status['unit'] == 'conversations' and bool(status['included'])
    if by_conversations:
        used, included = status['used'], status['included']
    else:
        used, included = used_row.get('messages', 0), included_messages(client, days)
    projection = round(used / elapsed * days) if elapsed else 0

    def pct_of(n):
        return round(n * 100 / included) if included else None

    pct, projection_pct = pct_of(used), pct_of(projection)
    cap = today_cap_usage([client]).get(client.pk) or {'used': 0, 'cap': 0, 'pct': None}
    return {
        'used': used, 'included': included, 'pct': pct,
        'bar_pct': min(100, pct) if pct is not None else 0,
        'warn': pct is not None and pct >= ALLOWANCE_WARN_PCT,
        'projection': projection, 'projection_pct': projection_pct,
        'projection_warn': projection_pct is not None and projection_pct >= 100,
        'days': days, 'elapsed': elapsed,
        'today': cap['used'], 'today_cap': cap['cap'], 'today_pct': cap['pct'],
        'today_warn': cap['pct'] is not None and cap['pct'] >= ALLOWANCE_WARN_PCT,
        'tokens': used_row.get('tokens', 0),
        'unit': 'conversations' if by_conversations else 'messages',
        'unit_label': 'conversaciones' if by_conversations else 'mensajes',
        'messages': used_row.get('messages', 0),
        # Overage detail (blocks, cents, WhatsApp, seats) for the plan card.
        'overage': status if by_conversations else None,
    }


def business_hours(client_ids):
    """{client_id: {weekday: (open, close)}} from AvailabilityRule. Clients
    absent from the result have no rules (use DEFAULT_HOURS). One query."""
    out = {}
    rows = (_scoped(AvailabilityRule.objects.all(), client_ids).order_by()
            .values_list('client_id', 'weekday', 'open_time', 'close_time'))
    for cid, weekday, open_t, close_t in rows:
        out.setdefault(cid, {})[weekday] = (open_t, close_t)
    return out


def is_after_hours(moment, hours):
    """True when an aware datetime falls outside `hours` ({weekday: (open,
    close)}), evaluated in Puerto Rico time. A weekday with no window is
    closed all day."""
    local = timezone.localtime(moment, BUSINESS_TZ)
    window = hours.get(local.weekday())
    if window is None:
        return True
    open_t, close_t = window
    return not (open_t <= local.time() < close_t)


def after_hours(client_ids, start, end, rules=None):
    """{client_id: n} conversations started outside each client's business
    hours in [start, end). Two queries (started_at values + rules), computed
    in Python so it stays portable across databases and time zones. Pass
    `rules` (a business_hours() result) when the caller already loaded it."""
    if rules is None:
        rules = business_hours(client_ids)
    out = {}
    rows = (_scoped(Conversation.objects.filter(started_at__gte=start, started_at__lt=end),
                    client_ids)
            .order_by().values_list('client_id', 'started_at'))
    for cid, started in rows.iterator():
        if cid not in out:
            out[cid] = 0
        if is_after_hours(started, rules.get(cid) or DEFAULT_HOURS):
            out[cid] += 1
    return out


def months_in_period(start, end):
    """1 for an exact calendar month, otherwise the window length in 30-day
    months (30d -> 1, 90d -> 3). Used to compare a monthly price with a period."""
    month_start, month_end = usage_ledger.month_bounds(start)
    if start == month_start and end == month_end:
        return Decimal(1)
    return (Decimal((end - start).total_seconds()) / Decimal(30 * 86400)).quantize(_CENT)


def value_metrics(client, start, end, vol=None, after=None):
    """What the client pays vs what its agent did in [start, end).

    Based on the PRICE THE CLIENT PAYS (plan_monthly_revenue), never on
    DOMINIO's AI cost. 'price' is None when unknown or $0 (no paid plan):
    the template hides the value card then. cost_per_lead /
    cost_per_conversation are None when the divisor is zero (shown as "—").
    Pass `vol` (a volume() row) and `after` (an after_hours() count) to reuse
    figures the caller already has; otherwise they are queried here."""
    price = plan_monthly_revenue(_subscription_of(client))
    if vol is None:
        vol = volume([client.pk], start, end).get(client.pk) or _empty_volume()
    if after is None:
        after = after_hours([client.pk], start, end).get(client.pk, 0)
    conversations, leads = vol['conversations'], vol['chat_leads']
    known = price is not None and price > 0
    paid = (price * months_in_period(start, end)).quantize(_CENT) if known else None

    def per(n):
        return (paid / n).quantize(_CENT) if known and n else None

    return {
        'price': price if known else None, 'paid': paid,
        'months': months_in_period(start, end),
        'conversations': conversations, 'after_hours': after, 'leads': leads,
        'cost_per_lead': per(leads), 'cost_per_conversation': per(conversations),
    }


def first_user_turns(conversation_ids, turns=3):
    """{conversation_id: [content, ...]} with the first `turns` user messages
    of each conversation, in position order. One query."""
    out = {}
    # Turns are stored as user/assistant pairs, so the first N user turns sit
    # well inside the first 4N positions; the bound keeps long chats cheap.
    rows = (ChatMessage.objects.filter(conversation_id__in=list(conversation_ids), role='user',
                                       position__lt=turns * 4)
            .order_by('conversation_id', 'position').values_list('conversation_id', 'content'))
    for conv_id, content in rows:
        bucket = out.setdefault(conv_id, [])
        if len(bucket) < turns:
            bucket.append(content)
    return out
