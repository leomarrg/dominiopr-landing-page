"""Platform analytics: volume, intents, revenue and config per tenant.

Shared by the staff control center (landing.control_views) and, next, the
tenant dashboard. Two rules keep it safe to run with hundreds of clients:

  * every function issues a FIXED number of grouped queries, never one per
    client; results are merged in Python keyed by client_id;
  * `client_ids=None` means "all clients" and is only for staff callers. A
    tenant caller must always pass its own member client ids.

Only portable ORM constructs are used (SQLite now, Postgres later).
"""
import math
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db.models import Avg, Count, Exists, OuterRef, Q, Sum
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


def _visitor_conversations(qs):
    """Phase E: every conversation figure here is about VISITORS (web today,
    WhatsApp later). An employee's chat with the internal assistant
    (channel='internal') is reported on its own line by the caller, never
    mixed into volume, daily bars, after-hours or the "what visitors ask"
    lists (its topics would leak internal matters to the agent role)."""
    return qs.exclude(channel='internal')


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
    """Per-client VISITOR activity in [start, end). Five grouped queries in
    total. Internal-assistant conversations are excluded (see
    _visitor_conversations).

    'chat_leads' = leads the agent itself captured (chat or WhatsApp).
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

    convs = (_scoped(_visitor_conversations(
                         Conversation.objects.filter(started_at__gte=start, started_at__lt=end)),
                     client_ids)
             .order_by().values('client_id')
             .annotate(n=Count('id'), esc=Count('id', filter=Q(state='escalated'))))
    for r in convs:
        row = row_for(r['client_id'])
        row['conversations'], row['escalated'] = r['n'], r['esc']

    leads = (_scoped(ContactSubmission.objects.filter(created_at__gte=start, created_at__lt=end),
                     client_ids)
             .order_by().values('client_id')
             .annotate(n=Count('id'),
                       chat=Count('id', filter=Q(source__in=ContactSubmission.AGENT_SOURCES))))
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
        _visitor_conversations(
            Conversation.objects.filter(client_id=client_id, started_at__gte=since))
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
    rows = (_scoped(_visitor_conversations(
                        Conversation.objects.filter(started_at__gte=start, started_at__lt=end,
                                                    tagged_at__isnull=False)),
                    client_ids)
            .exclude(intent='').order_by().values('intent')
            .annotate(n=Count('id')).order_by('-n', 'intent'))
    return [{'intent': r['intent'], 'label': INTENT_LABELS.get(r['intent'], r['intent']),
             'n': r['n']} for r in rows]


def top_topics(client_ids, start, end, n=10):
    """[{'topic', 'n'}], most frequent first. Clinic clients never have a
    topic, so they contribute nothing. One query."""
    rows = (_scoped(_visitor_conversations(
                        Conversation.objects.filter(started_at__gte=start, started_at__lt=end)),
                    client_ids)
            .exclude(topic='').order_by().values('topic')
            .annotate(n=Count('id')).order_by('-n', 'topic')[:n])
    return list(rows)


def recent_first_questions(client_id, n=20):
    """Latest opening questions of one client. Always empty for clinic_mode
    clients (enforced in the query, not only by the template). One query."""
    return list(
        _visitor_conversations(
            Conversation.objects.filter(client_id=client_id, client__clinic_mode=False))
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
    absent from the result have no rules (use DEFAULT_HOURS). One query.
    Phase F: business-wide rules only; a location's own window must not
    overwrite the business's in the after-hours report."""
    out = {}
    rows = (_scoped(AvailabilityRule.objects.filter(location__isnull=True), client_ids)
            .order_by().values_list('client_id', 'weekday', 'open_time', 'close_time'))
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
    rows = (_scoped(_visitor_conversations(
                        Conversation.objects.filter(started_at__gte=start, started_at__lt=end)),
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


# ------------------------------------------------------------------
# CRM dashboard (home / leads / reports): per-channel series, deltas,
# funnel, peak hours, CSAT, resolution, attention band and SVG geometry.
# ------------------------------------------------------------------
# Same two rules as above: a fixed number of grouped queries per call, and
# nothing here returns a DOMINIO cost figure (safe for tenants). Every date
# bucket is a Puerto Rico calendar day (BUSINESS_TZ).

CHANNEL_LABELS = {'web': 'Web', 'whatsapp': 'WhatsApp'}
WEEKDAYS_ES = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']
# Below this many conversations the 7x24 grid is too sparse to read: the
# hours are grouped in 3 h blocks (7x8).
PEAK_BUCKET_MIN_TOTAL = 50
STALE_NEW_HOURS = 24
CLOSED_STATES = ('closed_resolved', 'closed_unresolved')

KPI_DEFS = [
    ('conversations', 'Conversaciones', ''),
    ('leads', 'Leads', ''),
    ('bookings', 'Citas', ''),
    ('conversion', 'Conversión a lead', '%'),
    ('escalated', 'Escaladas', ''),
    ('csat', 'Satisfacción', '/5'),
]


def _cid(client):
    return getattr(client, 'pk', client)


def previous_period(start, end):
    """The window of the same length right before [start, end)."""
    span = end - start
    return start - span, start


def _local_window(days):
    """(first_day, since_aware, today) for the last `days` Puerto Rico days."""
    today = timezone.localtime(timezone.now(), BUSINESS_TZ).date()
    first = today - timedelta(days=max(1, days) - 1)
    since = datetime(first.year, first.month, first.day, tzinfo=BUSINESS_TZ)
    return first, since, today


def _day_bounds_local(day):
    start = datetime(day.year, day.month, day.day, tzinfo=BUSINESS_TZ)
    return start, start + timedelta(days=1)


def daily_by_channel(client, days=30):
    """Visitor conversations per Puerto Rico day and channel for one client,
    oldest first, zero-filled. One grouped query. Items: {'day', 'web',
    'whatsapp', 'total'} (any other visitor channel is folded into 'web')."""
    first, since, _today = _local_window(days)
    rows = (_visitor_conversations(
                Conversation.objects.filter(client_id=_cid(client), started_at__gte=since))
            .order_by().annotate(day=TruncDate('started_at', tzinfo=BUSINESS_TZ))
            .values('day', 'channel').annotate(n=Count('id')))
    by_day = {}
    for r in rows:
        by_day.setdefault(r['day'], {})[r['channel']] = r['n']
    series = []
    for i in range(max(1, days)):
        day = first + timedelta(days=i)
        d = by_day.get(day, {})
        wa = d.get('whatsapp', 0)
        web = sum(n for ch, n in d.items() if ch != 'whatsapp')
        series.append({'day': day, 'web': web, 'whatsapp': wa, 'total': web + wa})
    return series


def daily_counts(client, days=30, kind='leads'):
    """[{'day', 'value'}] per Puerto Rico day, oldest first, zero-filled, for
    one client. kind: 'leads' (ContactSubmission.created_at), 'bookings'
    (Booking.created_at), 'conversations' or 'escalated' (visitor
    conversations by started_at). One grouped query."""
    first, since, _today = _local_window(days)
    cid = _cid(client)
    if kind == 'leads':
        qs = ContactSubmission.objects.filter(client_id=cid, created_at__gte=since)
        field = 'created_at'
    elif kind == 'bookings':
        qs = Booking.objects.filter(client_id=cid, created_at__gte=since)
        field = 'created_at'
    elif kind == 'escalated':
        qs = _visitor_conversations(Conversation.objects.filter(
            client_id=cid, started_at__gte=since, state='escalated'))
        field = 'started_at'
    elif kind == 'conversations':
        qs = _visitor_conversations(Conversation.objects.filter(client_id=cid, started_at__gte=since))
        field = 'started_at'
    else:
        raise ValueError(f'Unknown daily_counts kind: {kind!r}')
    rows = dict(qs.order_by().annotate(day=TruncDate(field, tzinfo=BUSINESS_TZ))
                .values('day').annotate(n=Count('id')).values_list('day', 'n'))
    return [{'day': first + timedelta(days=i), 'value': rows.get(first + timedelta(days=i), 0)}
            for i in range(max(1, days))]


def delta(cur, prev):
    """{'value', 'prev', 'pct', 'direction'} comparing a figure with the
    previous period. pct is None when there is no previous figure to compare
    against (template shows "nuevo" / "—"), 0 when both are zero."""
    c = cur or 0
    p = prev or 0
    if p:
        pct = round((c - p) * 100 / p)
    else:
        pct = None if c else 0
    direction = 'up' if c > p else ('down' if c < p else 'flat')
    return {'value': cur, 'prev': prev, 'pct': pct, 'direction': direction}


def kpi_counts(client, start, end):
    """Tenant-safe activity figures of one client in [start, end): four
    grouped queries, no usage ledger. Keys: conversations, escalated,
    with_lead, leads, agent_leads, bookings, csat_avg, csat_n, conversion
    (agent_leads as % of conversations)."""
    cid = _cid(client)
    conv = (_visitor_conversations(Conversation.objects.filter(
                client_id=cid, started_at__gte=start, started_at__lt=end))
            .aggregate(n=Count('id'), esc=Count('id', filter=Q(state='escalated')),
                       with_lead=Count('id', filter=Q(lead__isnull=False))))
    leads = (ContactSubmission.objects.filter(client_id=cid, created_at__gte=start,
                                              created_at__lt=end)
             .aggregate(n=Count('id'),
                        agent=Count('id', filter=Q(source__in=ContactSubmission.AGENT_SOURCES))))
    bookings = Booking.objects.filter(client_id=cid, created_at__gte=start,
                                      created_at__lt=end).aggregate(n=Count('id'))
    csat = Survey.objects.filter(conversation__client_id=cid, created_at__gte=start,
                                 created_at__lt=end).aggregate(avg=Avg('score'), n=Count('id'))
    conversations = conv['n'] or 0
    agent_leads = leads['agent'] or 0
    return {
        'conversations': conversations, 'escalated': conv['esc'] or 0,
        'with_lead': conv['with_lead'] or 0,
        'leads': leads['n'] or 0, 'agent_leads': agent_leads,
        'bookings': bookings['n'] or 0,
        'csat_avg': round(csat['avg'], 1) if csat['avg'] is not None else None,
        'csat_n': csat['n'] or 0,
        'conversion': round(agent_leads * 100 / conversations) if conversations else 0,
    }


def kpis(client, start, end, cur=None, prev=None):
    """The six headline KPIs with their delta vs the previous period of the
    same length: [{'key', 'label', 'suffix', 'value', 'prev', 'pct',
    'direction'}]. Eight queries (two kpi_counts) unless both are passed."""
    cur = cur or kpi_counts(client, start, end)
    if prev is None:
        p_start, p_end = previous_period(start, end)
        prev = kpi_counts(client, p_start, p_end)
    out = []
    for key, label, suffix in KPI_DEFS:
        field = 'csat_avg' if key == 'csat' else key
        item = {'key': key, 'label': label, 'suffix': suffix}
        item.update(delta(cur[field], prev[field]))
        out.append(item)
    return out


def funnel(client, start, end):
    """Cohort funnel of the visitor conversations STARTED in [start, end):
    all -> with a lead (Conversation.lead) -> with a booking (a Booking of
    the same client linked to that lead, or with the lead's email). One
    query. Returns {'stages': [{'key', 'label', 'n', 'pct'}],
    'conversations', 'leads', 'bookings', 'lead_rate', 'booking_rate',
    'has_bookings_stage'}; pct is relative to the first stage."""
    cid = _cid(client)
    booked = (Booking.objects.filter(client_id=OuterRef('client_id'))
              .filter(Q(lead_id=OuterRef('lead_id'))
                      | (~Q(email='') & Q(email__iexact=OuterRef('lead__email')))))
    agg = (_visitor_conversations(Conversation.objects.filter(
               client_id=cid, started_at__gte=start, started_at__lt=end))
           .annotate(booked=Exists(booked))
           .aggregate(total=Count('id'),
                      with_lead=Count('id', filter=Q(lead__isnull=False)),
                      with_booking=Count('id', filter=Q(lead__isnull=False, booked=True))))
    total = agg['total'] or 0
    with_lead = agg['with_lead'] or 0
    with_booking = agg['with_booking'] or 0

    def pct(n):
        return round(n * 100 / total) if total else 0

    stages = [
        {'key': 'conversations', 'label': 'Conversaciones', 'n': total,
         'pct': 100 if total else 0},
        {'key': 'leads', 'label': 'Con lead', 'n': with_lead, 'pct': pct(with_lead)},
        {'key': 'bookings', 'label': 'Con cita', 'n': with_booking, 'pct': pct(with_booking)},
    ]
    return {
        'stages': stages, 'conversations': total, 'leads': with_lead, 'bookings': with_booking,
        'lead_rate': pct(with_lead), 'booking_rate': pct(with_booking),
        'has_bookings_stage': bool(getattr(client, 'enable_bookings', True)),
    }


def peak_hours(client, start, end):
    """When visitors write: a weekday x hour matrix (Puerto Rico time) of the
    visitor conversations started in [start, end). One query (started_at
    values), bucketed in Python so it stays portable. With fewer than
    PEAK_BUCKET_MIN_TOTAL conversations the hours are grouped in 3 h blocks
    ('bucket_hours' = 3, 8 columns). Returns {'matrix' (7 rows), 'days',
    'hours' (column labels), 'bucket_hours', 'total', 'max', 'top'
    ([{'day', 'hour', 'n'}], busiest 3 cells)}."""
    rows = (_visitor_conversations(Conversation.objects.filter(
                client_id=_cid(client), started_at__gte=start, started_at__lt=end))
            .order_by().values_list('started_at', flat=True))
    matrix = [[0] * 24 for _ in range(7)]
    total = 0
    for started in rows.iterator():
        local = timezone.localtime(started, BUSINESS_TZ)
        matrix[local.weekday()][local.hour] += 1
        total += 1
    bucket = 1 if total >= PEAK_BUCKET_MIN_TOTAL else 3
    if bucket == 3:
        matrix = [[sum(row[h:h + 3]) for h in range(0, 24, 3)] for row in matrix]
        hours = [f'{h}–{h + 3}' for h in range(0, 24, 3)]
    else:
        hours = [f'{h}:00' for h in range(24)]
    peak = max((n for row in matrix for n in row), default=0)
    cells = [(n, d, h) for d, row in enumerate(matrix) for h, n in enumerate(row) if n]
    cells.sort(key=lambda c: (-c[0], c[1], c[2]))
    top = [{'day': WEEKDAYS_ES[d], 'hour': hours[h], 'n': n} for n, d, h in cells[:3]]
    return {'matrix': matrix, 'days': list(WEEKDAYS_ES), 'hours': hours,
            'bucket_hours': bucket, 'total': total, 'max': peak, 'top': top}


def csat_distribution(client, start, end, clinic=False):
    """CSAT 1-5 of one client in [start, end): {'counts': {1..5: n}, 'rows'
    ([{'score', 'n', 'pct'}], 5 first), 'n', 'avg', 'comments' ([{'score',
    'comment', 'created_at'}], latest 5)}. Two queries. Comments are never
    returned for a clinic_mode client (free text may carry health data),
    whatever the caller passes."""
    cid = _cid(client)
    clinic = bool(clinic or getattr(client, 'clinic_mode', False))
    base = Survey.objects.filter(conversation__client_id=cid, created_at__gte=start,
                                 created_at__lt=end)
    got = dict(base.order_by().values('score').annotate(n=Count('id')).values_list('score', 'n'))
    counts = {score: got.get(score, 0) for score in range(1, 6)}
    n = sum(counts.values())
    avg = round(sum(score * c for score, c in counts.items()) / n, 1) if n else None
    rows = [{'score': score, 'n': counts[score], 'pct': round(counts[score] * 100 / n) if n else 0}
            for score in (5, 4, 3, 2, 1)]
    comments = [] if clinic else list(
        base.exclude(comment='').order_by('-created_at', '-id')  # same instant: newest row first
        .values('score', 'comment', 'created_at')[:5])
    return {'counts': counts, 'rows': rows, 'n': n, 'avg': avg, 'comments': comments}


def resolution_rate(client, start, end):
    """How many of the conversations the team closed were resolved:
    {'resolved', 'unresolved', 'closed', 'pct'} (pct None without closed
    conversations). One query, over visitor conversations started in the
    period."""
    agg = (_visitor_conversations(Conversation.objects.filter(
               client_id=_cid(client), started_at__gte=start, started_at__lt=end))
           .aggregate(resolved=Count('id', filter=Q(state='closed_resolved')),
                      unresolved=Count('id', filter=Q(state='closed_unresolved'))))
    resolved, unresolved = agg['resolved'] or 0, agg['unresolved'] or 0
    closed = resolved + unresolved
    return {'resolved': resolved, 'unresolved': unresolved, 'closed': closed,
            'pct': round(resolved * 100 / closed) if closed else None}


def attention(client, user=None):
    """The "Requiere atención" band: {'items': [{'key', 'label', 'count',
    'mine', 'url', 'tone'}], 'total'}. Three grouped queries. Each URL opens
    the list already filtered. 'mine' = of those, the ones assigned to
    `user` (0 without a user)."""
    from django.urls import reverse  # local: keeps this module import-light
    cid = _cid(client)
    slug = getattr(client, 'slug', '')
    now = timezone.now()
    today = timezone.localtime(now, BUSINESS_TZ).date()
    day_start, day_end = _day_bounds_local(today)
    stale_before = now - timedelta(hours=STALE_NEW_HOURS)
    user_id = getattr(user, 'pk', None)
    mine = Q(assigned_to_id=user_id) if user_id else Q(pk__in=[])
    open_fu = Q(follow_up_done_at__isnull=True)
    stale = Q(status='new', created_at__lt=stale_before)
    overdue = open_fu & Q(follow_up_at__lt=day_start)
    today_q = open_fu & Q(follow_up_at__gte=day_start, follow_up_at__lt=day_end)
    leads = ContactSubmission.objects.filter(client_id=cid).aggregate(
        stale=Count('id', filter=stale), stale_mine=Count('id', filter=stale & mine),
        overdue=Count('id', filter=overdue), overdue_mine=Count('id', filter=overdue & mine),
        today=Count('id', filter=today_q), today_mine=Count('id', filter=today_q & mine),
    )
    escalated = _visitor_conversations(
        Conversation.objects.filter(client_id=cid, state='escalated')).count()
    pending = Booking.objects.filter(client_id=cid, status='pending', start__gte=now).count()
    q = f'client={slug}&' if slug else ''
    items = [
        {'key': 'stale_new', 'label': f'Leads nuevos sin atender (más de {STALE_NEW_HOURS} h)',
         'count': leads['stale'] or 0, 'mine': leads['stale_mine'] or 0, 'tone': 'danger',
         'url': reverse('dashboard') + f'?{q}stale=1'},
        {'key': 'escalated', 'label': 'Conversaciones escaladas abiertas',
         'count': escalated, 'mine': 0, 'tone': 'danger',
         'url': reverse('conversations') + f'?{q}state=escalated'},
        {'key': 'follow_up_overdue', 'label': 'Seguimientos vencidos',
         'count': leads['overdue'] or 0, 'mine': leads['overdue_mine'] or 0, 'tone': 'warn',
         'url': reverse('dashboard') + f'?{q}follow_up=overdue'},
        {'key': 'follow_up_today', 'label': 'Seguimientos para hoy',
         'count': leads['today'] or 0, 'mine': leads['today_mine'] or 0, 'tone': 'info',
         'url': reverse('dashboard') + f'?{q}follow_up=today'},
        {'key': 'bookings_pending', 'label': 'Citas por confirmar',
         'count': pending, 'mine': 0, 'tone': 'warn',
         'url': reverse('bookings') + f'?{q}status=pending'},
    ]
    return {'items': items, 'total': sum(i['count'] for i in items)}


def follow_up_lists(client, limit=10):
    """Open follow-ups of one client in three buckets for the home page:
    {'overdue': [...], 'today': [...], 'week': [...]} (leads, oldest
    follow-up first, `limit` each). One query."""
    cid = _cid(client)
    today = timezone.localtime(timezone.now(), BUSINESS_TZ).date()
    day_start, day_end = _day_bounds_local(today)
    week_end = day_start + timedelta(days=7)
    rows = list(ContactSubmission.objects
                .filter(client_id=cid, follow_up_at__isnull=False, follow_up_done_at__isnull=True,
                        follow_up_at__lt=week_end)
                .select_related('assigned_to').order_by('follow_up_at', 'pk')[:limit * 3])
    out = {'overdue': [], 'today': [], 'week': []}
    for lead in rows:
        key = ('overdue' if lead.follow_up_at < day_start
               else 'today' if lead.follow_up_at < day_end else 'week')
        if len(out[key]) < limit:
            out[key].append(lead)
    return out


# ---- SVG geometry (server-side charts; CSP is script-src 'self') ----------
# Every helper returns plain numbers/strings the template drops into
# attributes, plus 'rows' for the <details> "Ver como tabla" alternative.

def _fmt(v):
    return f'{v:.1f}'


def _nice_scale(peak, divisions=4):
    """(step, axis_max) so the axis has `divisions` equal, round steps that
    cover `peak` (5 -> step 2, max 8; 37 -> 10/40; 0 -> 1/4)."""
    peak = max(0, peak)
    raw = peak / divisions if peak else 1
    magnitude = 10 ** math.floor(math.log10(raw)) if raw >= 1 else 1
    step = magnitude
    for m in (1, 2, 2.5, 5, 10):
        step = m * magnitude
        if step * divisions >= peak:
            break
    step = int(step) if float(step).is_integer() else step
    return step, step * divisions


def sparkline(values, width=120, height=32):
    """A tiny trend line: {'viewBox', 'width', 'height', 'path', 'area',
    'last', 'max', 'points'}. Flat when every value is 0."""
    vals = [float(v or 0) for v in values]
    box = f'0 0 {width} {height}'
    if not vals:
        return {'viewBox': box, 'width': width, 'height': height, 'path': '', 'area': '',
                'last': 0, 'max': 0, 'points': []}
    peak = max(vals) or 1.0
    n = len(vals)
    step = width / (n - 1) if n > 1 else 0
    pts = [(i * step if n > 1 else width / 2, height - 2 - (v / peak) * (height - 4))
           for i, v in enumerate(vals)]
    path = 'M' + ' L'.join(f'{_fmt(x)},{_fmt(y)}' for x, y in pts)
    area = path + f' L{_fmt(pts[-1][0])},{height} L{_fmt(pts[0][0])},{height} Z'
    return {'viewBox': box, 'width': width, 'height': height, 'path': path, 'area': area,
            'last': vals[-1], 'max': peak,
            'points': [{'x': _fmt(x), 'y': _fmt(y)} for x, y in pts]}


def svg_area(series, keys, width=600, height=160, labels=None):
    """Stacked area chart of `keys` over a daily `series` (daily_by_channel).
    Returns {'viewBox', 'width', 'height', 'plot' ({'x','y','w','h'}),
    'layers' ([{'key', 'label', 'area', 'line'}], first key at the bottom),
    'x_labels' ([{'x', 'text'}], 4), 'y_ticks' ([{'y', 'text'}], 5 = 4
    divisions), 'y_max', 'columns', 'rows' ([{'day', 'cells', 'total'}]),
    'total', 'empty'}."""
    labels = labels or CHANNEL_LABELS
    pad_l, pad_r, pad_t, pad_b = 34, 8, 8, 20
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(series)
    totals = [sum(d.get(k, 0) for k in keys) for d in series]
    peak = max(totals, default=0)
    step, y_max = _nice_scale(peak)

    def y_of(v):
        return pad_t + plot_h - (v / y_max) * plot_h

    xs = [pad_l + (i * plot_w / (n - 1) if n > 1 else plot_w / 2) for i in range(n)]
    layers = []
    below = [0] * n
    for key in keys:
        top = [below[i] + series[i].get(key, 0) for i in range(n)]
        if n:
            line = 'M' + ' L'.join(f'{_fmt(xs[i])},{_fmt(y_of(top[i]))}' for i in range(n))
            back = ' '.join(f'L{_fmt(xs[i])},{_fmt(y_of(below[i]))}' for i in reversed(range(n)))
            area = f'{line} {back} Z'
        else:
            line = area = ''
        layers.append({'key': key, 'label': labels.get(key, key), 'area': area, 'line': line})
        below = top
    idx = sorted({0, n // 3, (2 * n) // 3, n - 1}) if n else []
    x_labels = [{'x': _fmt(xs[i]), 'text': series[i]['day'].strftime('%d/%m')} for i in idx]
    y_ticks = [{'y': _fmt(y_of(step * k)), 'text': step * k} for k in range(5)]
    rows = [{'day': d['day'], 'cells': [d.get(k, 0) for k in keys], 'total': totals[i]}
            for i, d in enumerate(series)]
    return {'viewBox': f'0 0 {width} {height}', 'width': width, 'height': height,
            'plot': {'x': pad_l, 'y': pad_t, 'w': plot_w, 'h': plot_h},
            'layers': layers, 'x_labels': x_labels, 'y_ticks': y_ticks, 'y_max': y_max,
            'columns': [labels.get(k, k) for k in keys], 'rows': rows,
            'total': sum(totals), 'empty': peak == 0}


def svg_funnel(stages, width=600, height=150):
    """Horizontal funnel bars for funnel()['stages']: {'viewBox', 'width',
    'height', 'bars' ([{'key', 'label', 'n', 'pct', 'drop', 'x', 'y', 'w',
    'h', 'text_y'}]), 'rows'}. 'drop' = % lost vs the previous stage (None
    for the first stage)."""
    n = len(stages)
    gap = 10
    bar_h = (height - gap * (n - 1)) / n if n else height
    base = stages[0]['n'] if stages and stages[0]['n'] else 0
    bars, rows, prev = [], [], None
    for i, st in enumerate(stages):
        w = (width * st['n'] / base) if base else 0
        if st['n'] and w < 3:
            w = 3
        if prev is None:
            drop = None
        elif prev:
            drop = round((prev - st['n']) * 100 / prev)
        else:
            drop = 0
        y = i * (bar_h + gap)
        bars.append({'key': st['key'], 'label': st['label'], 'n': st['n'], 'pct': st['pct'],
                     'drop': drop, 'x': 0, 'y': _fmt(y), 'w': _fmt(w), 'h': _fmt(bar_h),
                     'text_y': _fmt(y + bar_h / 2 + 4)})
        rows.append({'label': st['label'], 'n': st['n'], 'pct': st['pct'], 'drop': drop})
        prev = st['n']
    return {'viewBox': f'0 0 {width} {height}', 'width': width, 'height': height,
            'bars': bars, 'rows': rows}


def svg_heatmap(peak, width=600, height=170):
    """Cells for peak_hours(): {'viewBox', 'width', 'height', 'cells'
    ([{'x', 'y', 'w', 'h', 'level' (0-4), 'n', 'day', 'hour'}]),
    'row_labels' ([{'y', 'text'}]), 'col_labels' ([{'x', 'text'}]),
    'columns', 'rows' ([{'day', 'cells'}]), 'max', 'total', 'bucket_hours',
    'empty'}. level 0 = no conversations, 4 = the busiest cell."""
    matrix, hours, days = peak['matrix'], peak['hours'], peak['days']
    n_cols = len(hours)
    label_w, label_h = 34, 16
    cell_w = (width - label_w) / n_cols if n_cols else 0
    cell_h = (height - label_h) / len(days) if days else 0
    top = peak['max'] or 0
    cells = []
    for r, row in enumerate(matrix):
        for c, n in enumerate(row):
            level = max(1, math.ceil(n * 4 / top)) if (n and top) else 0
            cells.append({'x': _fmt(label_w + c * cell_w), 'y': _fmt(label_h + r * cell_h),
                          'w': _fmt(max(cell_w - 1, 1)), 'h': _fmt(max(cell_h - 1, 1)),
                          'level': level, 'n': n, 'day': days[r], 'hour': hours[c]})
    row_labels = [{'y': _fmt(label_h + r * cell_h + cell_h / 2 + 4), 'text': d}
                  for r, d in enumerate(days)]
    every = 3 if n_cols > 12 else 1
    col_labels = [{'x': _fmt(label_w + c * cell_w + cell_w / 2), 'text': h}
                  for c, h in enumerate(hours) if c % every == 0]
    rows = [{'day': days[r], 'cells': list(row)} for r, row in enumerate(matrix)]
    return {'viewBox': f'0 0 {width} {height}', 'width': width, 'height': height,
            'cells': cells, 'row_labels': row_labels, 'col_labels': col_labels,
            'columns': list(hours), 'rows': rows, 'max': top, 'total': peak['total'],
            'bucket_hours': peak['bucket_hours'], 'empty': peak['total'] == 0}
