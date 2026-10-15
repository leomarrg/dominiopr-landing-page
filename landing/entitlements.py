"""Phase C -- what a client's plan includes, what they used this month, and
what the overage costs.

Unit: a CONVERSATION (one Conversation row) STARTED in the calendar month
(America/Puerto_Rico, reset on the 1st) that got AT LEAST ONE assistant
reply. A row with no reply (widget opened, bot probing, a 429/503 before
the model answered) is never a billable unit. The launch plans
(starter/pro/scale/custom) are still measured by their daily message cap
(unit='messages'); the packages (small/medium/large/premium) include
`monthly_conversations` web conversations (channel 'web' only),
`wa_conversations` WhatsApp conversations and `seats` internal seats per
month. Internal-assistant conversations (channel 'internal') are counted
and reported but never billed.

The service is NEVER cut on overage. At 80% and 100% of each sold bucket
(web, WhatsApp) the client and DOMINIO get one email each per month
(check_thresholds, called after every first turn of a conversation); at
month end `manage.py bill_overage` charges the extra blocks as Stripe
invoice items.

Canonical source for the numbers: views.PLAN_LIMITS / views.OVERAGE_PRICES
(imported lazily: views imports turns, which imports this module), merged
with Client.entitlement_overrides for custom deals.
"""
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef
from django.urls import reverse
from django.utils import timezone

from .models import AuditEvent, ChatMessage, Conversation, Membership

logger = logging.getLogger(__name__)

BUSINESS_TZ = ZoneInfo('America/Puerto_Rico')
THRESHOLDS = (80, 100)
# Capacity buckets that get 80% / 100% warnings: (key in allowance_status,
# AuditEvent action prefix, human label).
BUCKETS = (('web', 'allowance', 'conversaciones'),
           ('wa', 'allowance.wa', 'conversaciones de WhatsApp'))
# Override clamps: nothing in a per-client JSON can make a counter absurd.
MAX_OVERRIDE_INT = 10 ** 7
MIN_BLOCK_CENTS = 100
# Membership roles that do NOT consume an internal seat (dashboard users).
# Phase E: the 'employee' role (internal assistant only) is the one that
# takes a seat; org_admin and agent use the assistant without one.
NON_SEAT_ROLES = ('org_admin', 'agent')
# Cache keys live ~40 days so a threshold fires once per month even if the
# AuditEvent lookup is skipped; the AuditEvent is the durable record.
_THRESHOLD_TTL = 60 * 60 * 24 * 40


def _plans():
    from . import views  # late import, see module docstring
    return views


def _subscription_of(client):
    return getattr(client, 'subscription', None)


# ------------------------------------------------------------------
# Entitlements
# ------------------------------------------------------------------

def _coerce_int(value):
    if isinstance(value, bool) or value is None:
        raise ValueError(value)
    return min(MAX_OVERRIDE_INT, max(0, int(value)))


def _coerce_channels(value):
    if isinstance(value, str):
        value = [value]
    return {str(v) for v in value if str(v) in dict(Conversation.CHANNEL_CHOICES)}


def _coerce_overage(value):
    out = {}
    for kind, pair in dict(value).items():
        size, cents = pair
        size, cents = _coerce_int(size), _coerce_int(cents)
        if size <= 0 or cents < MIN_BLOCK_CENTS:
            # A free or near-free block would let an override bill nothing (or
            # pennies) for unlimited overage; a 0-size block divides by zero.
            raise ValueError(pair)
        out[str(kind)] = (size, cents)
    return out


def _coerce_unit(value):
    if value not in ('messages', 'conversations'):
        raise ValueError(value)
    return value


OVERRIDE_KEYS = {
    'monthly_conversations': _coerce_int,
    'wa_conversations': _coerce_int,
    'seats': _coerce_int,
    'locations': _coerce_int,
    'channels': _coerce_channels,
    'overage': _coerce_overage,
    'unit': _coerce_unit,
}


def entitlements(client):
    """What `client` is entitled to this month: the plan table at the
    subscription's step, with Client.entitlement_overrides applied on top.
    Unknown override keys and malformed values are ignored (logged), so a
    typo in the admin can never take a client's capacity to zero.

    Keys: unit ('messages' | 'conversations'), plan, step,
    monthly_conversations (None for 'messages' plans), wa_conversations,
    seats, locations, channels (set), overage ({kind: (size, cents)}),
    daily_message_cap, enable_bookings."""
    views = _plans()
    sub = _subscription_of(client)
    ent = views.plan_limits(sub.plan if sub else None, sub.step if sub else 1)
    ent['plan'] = sub.plan if sub else ''
    ent.setdefault('step', 1)
    ent.setdefault('unit', 'messages')
    ent.setdefault('monthly_conversations', None)
    ent.setdefault('wa_conversations', 0)
    ent.setdefault('seats', 0)
    ent.setdefault('locations', 1)
    ent.setdefault('channels', {'web'})
    ent.setdefault('overage', dict(views.OVERAGE_PRICES))

    overrides = getattr(client, 'entitlement_overrides', None) or {}
    if not isinstance(overrides, dict):
        logger.warning('entitlement_overrides of %s is not an object; ignored', client.slug)
        return ent
    for key, value in overrides.items():
        coerce = OVERRIDE_KEYS.get(key)
        if coerce is None:
            logger.warning('Unknown entitlement override %r on %s; ignored', key, client.slug)
            continue
        try:
            ent[key] = coerce(value)
        except (TypeError, ValueError):
            logger.warning('Bad entitlement override %s=%r on %s; ignored', key, value, client.slug)
    if ent.get('monthly_conversations') and ent['unit'] == 'messages' and 'unit' not in overrides:
        # A monthly capacity given to a launch plan client switches the unit.
        ent['unit'] = 'conversations'
    if 'channels' in overrides and 'whatsapp' in ent['channels'] and not ent.get('wa_conversations'):
        # WhatsApp sold by override without a WhatsApp capacity would bill
        # every WhatsApp conversation as overage from the first one.
        logger.warning('Override sells whatsapp to %s without wa_conversations; channel ignored',
                       client.slug)
        ent['channels'] = set(ent['channels']) - {'whatsapp'}
    return ent


# ------------------------------------------------------------------
# Usage
# ------------------------------------------------------------------

def month_bounds(moment=None):
    """[start, end) of the calendar month containing `moment` (default now)
    in BUSINESS_TZ (America/Puerto_Rico), independent of settings.TIME_ZONE.
    `moment` may be a date or an aware datetime."""
    if moment is None:
        day = timezone.localtime(timezone.now(), BUSINESS_TZ).date()
    elif isinstance(moment, dt.datetime):
        day = (timezone.localtime(moment, BUSINESS_TZ).date()
               if timezone.is_aware(moment) else moment.date())
    else:
        day = moment
    start = dt.datetime(day.year, day.month, 1, tzinfo=BUSINESS_TZ)
    nxt_year, nxt_month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return start, dt.datetime(nxt_year, nxt_month, 1, tzinfo=BUSINESS_TZ)


def _usage_rows(client_ids, start, end):
    """(client_id, channel, n) for conversations started in [start, end)
    that received at least one assistant reply -- the billable unit (every
    bucket). Served by the (client, started_at) index on Conversation."""
    answered = ChatMessage.objects.filter(conversation=OuterRef('pk'), role='assistant')
    qs = (Conversation.objects.filter(started_at__gte=start, started_at__lt=end)
          .filter(Exists(answered)))
    if client_ids is not None:
        qs = qs.filter(client_id__in=list(client_ids))
    return (qs.order_by().values('client_id', 'channel').annotate(n=Count('id')))


def _seat_rows(client_ids):
    # A disabled login keeps its Membership (audit trail) but is not a seat.
    qs = Membership.objects.exclude(role__in=NON_SEAT_ROLES).filter(user__is_active=True)
    if client_ids is not None:
        qs = qs.filter(client_id__in=list(client_ids))
    return qs.order_by().values('client_id').annotate(n=Count('id'))


def _empty_usage(start, end):
    return {'conversations': 0, 'web': 0, 'whatsapp': 0, 'internal': 0, 'seats_used': 0,
            'start': start, 'end': end}


def month_usage_many(client_ids, month_start=None):
    """{client_id: month_usage(...)} for several clients in two grouped
    queries (control center). client_ids None = every client."""
    start, end = month_bounds(month_start)
    out = {}
    for row in _usage_rows(client_ids, start, end):
        entry = out.setdefault(row['client_id'], _empty_usage(start, end))
        entry['conversations'] += row['n']
        if row['channel'] in ('web', 'whatsapp', 'internal'):
            entry[row['channel']] += row['n']
    for row in _seat_rows(client_ids):
        out.setdefault(row['client_id'], _empty_usage(start, end))['seats_used'] = row['n']
    if client_ids is not None:
        for cid in client_ids:
            out.setdefault(cid, _empty_usage(start, end))
    return out


def month_usage(client, month_start=None):
    """Conversations STARTED in the month that got an assistant reply (all
    channels, plus the per-channel split) and the active internal seats in
    use. Keys: conversations, web, whatsapp, internal, seats_used, start,
    end. Only `web` and `whatsapp` are billable; `internal` is informational."""
    return month_usage_many([client.pk], month_start)[client.pk]


# ------------------------------------------------------------------
# Allowance / overage
# ------------------------------------------------------------------

def price_label(cents):
    """'$15' / '$149' / '$9.50' for a cents amount."""
    cents = int(cents or 0)
    return f'${cents // 100}' if cents % 100 == 0 else f'${cents / 100:,.2f}'


def _blocks(over_units, block):
    size, cents = block
    if over_units <= 0 or size <= 0:
        return 0, 0
    blocks = -(-over_units // size)   # ceil
    return blocks, blocks * cents


def _bucket(included, used, block, elapsed, days):
    """One capacity bucket (web conversations, WhatsApp conversations)."""
    projection = round(used / elapsed * days) if elapsed else 0
    over = max(0, used - included) if included is not None else 0
    blocks, cents = _blocks(over, block)
    pct = round(used * 100 / included) if included else None
    projection_pct = round(projection * 100 / included) if included else None
    return {
        'included': included, 'used': used, 'pct': pct,
        'bar_pct': min(100, pct) if pct is not None else 0,
        'remaining': max(0, included - used) if included is not None else None,
        'warn': pct is not None and pct >= THRESHOLDS[0],
        'reached': pct is not None and pct >= 100,
        'projection': projection, 'projection_pct': projection_pct,
        'projection_warn': projection_pct is not None and projection_pct >= 100,
        'overage_units': over, 'block_size': block[0], 'block_cents': block[1],
        'block_price': price_label(block[1]),
        'overage_blocks': blocks, 'overage_cents': cents, 'overage_price': price_label(cents),
    }


def allowance_status(client, month_start=None, ent=None, usage=None):
    """Included vs used for one calendar month, with the overage the client
    will be billed at month end. Never cuts anything; purely informational.

    Top-level keys describe the WEB bucket (channel 'web' only; WhatsApp has
    its own `wa` bucket and 'internal' is reported but never billed): unit,
    unit_label, plan,
    included, used, pct, bar_pct, remaining, warn, reached, projection,
    projection_pct, projection_warn, overage_units, block_size, block_cents,
    block_price, overage_blocks, overage_cents. Plus `wa`, `seats`,
    total_overage_cents, billable, days, elapsed, month_start, month_end,
    month_label ('YYYY-MM'), and the raw split: all_conversations, web,
    whatsapp, internal. For 'messages' plans `included` is None and every
    overage figure is 0 (those plans are not billed per conversation)."""
    ent = ent or entitlements(client)
    usage = usage or month_usage(client, month_start)
    start, end = usage['start'], usage['end']
    first_day = timezone.localtime(start, BUSINESS_TZ).date()
    days = max(1, (timezone.localtime(end, BUSINESS_TZ).date() - first_day).days)
    today = timezone.localtime(timezone.now(), BUSINESS_TZ).date()
    elapsed = min(days, max(0, (today - first_day).days + 1))
    monthly = ent.get('monthly_conversations') if ent['unit'] == 'conversations' else None
    overage = ent.get('overage') or {}

    web_used = usage['web']
    web = _bucket(monthly, web_used, overage.get('web_block', (100, 1500)), elapsed, days)
    if 'whatsapp' in ent.get('channels', ()) and monthly is not None:
        wa_included = int(ent.get('wa_conversations') or 0)
    else:
        wa_included = None   # channel not sold on this plan: never billed by this path
    wa = _bucket(wa_included, usage['whatsapp'], overage.get('wa_block', (500, 14900)),
                 elapsed, days)
    seats_included = int(ent.get('seats') or 0)
    seat_extra = max(0, usage['seats_used'] - seats_included) if monthly is not None else 0
    seat_cents = seat_extra * (overage.get('seat', (1, 900))[1])
    seats = {'included': seats_included, 'used': usage['seats_used'], 'extra': seat_extra,
             'cents': seat_cents, 'price': price_label(overage.get('seat', (1, 900))[1])}

    total = web['overage_cents'] + wa['overage_cents'] + seat_cents
    out = dict(web)
    out.update({
        'unit': ent['unit'], 'plan': ent.get('plan', ''), 'step': ent.get('step', 1),
        'unit_label': 'conversaciones' if ent['unit'] == 'conversations' else 'mensajes',
        'wa': wa, 'seats': seats,
        'total_overage_cents': total, 'total_overage_price': price_label(total),
        'billable': total > 0,
        'days': days, 'elapsed': elapsed,
        'month_start': start, 'month_end': end, 'month_label': first_day.strftime('%Y-%m'),
        'all_conversations': usage['conversations'], 'web': usage['web'],
        'whatsapp': usage['whatsapp'], 'internal': usage['internal'],
    })
    return out


def overage_items(status):
    """The billable lines of an allowance_status: [(kind, units, cents,
    description)]. Empty when nothing is owed."""
    label = status['month_label']
    items = []
    if status['overage_blocks']:
        n = status['overage_blocks'] * status['block_size']
        items.append(('web_block', status['overage_blocks'], status['overage_cents'],
                      f'DOMINIO — {n} conversaciones adicionales (mes {label})'))
    wa = status['wa']
    if wa['overage_blocks']:
        n = wa['overage_blocks'] * wa['block_size']
        items.append(('wa_block', wa['overage_blocks'], wa['overage_cents'],
                      f'DOMINIO — {n} conversaciones de WhatsApp adicionales (mes {label})'))
    seats = status['seats']
    if seats['extra']:
        items.append(('seat', seats['extra'], seats['cents'],
                      f'DOMINIO — {seats["extra"]} asiento(s) interno(s) adicional(es) (mes {label})'))
    return items


# ------------------------------------------------------------------
# 80% / 100% notifications
# ------------------------------------------------------------------

def threshold_key(client_pk, month_label, threshold, bucket='web'):
    return f'allow:{client_pk}:{month_label.replace("-", "")}:{bucket}:{threshold}'


def _record_threshold(client, action, label, result):
    """Durable once-per-month record of a fired threshold. True when this
    call created it. The partial UniqueConstraint on AuditEvent
    (client, action, target) for 'allowance.*' makes two first turns racing
    past the cache collapse into one email: the loser gets IntegrityError
    inside its own savepoint and backs off."""
    if AuditEvent.objects.filter(client=client, action=action, target=label).exists():
        return False
    try:
        with transaction.atomic():
            AuditEvent.objects.create(client=client, action=action, target=label, result=result)
    except IntegrityError:
        return False
    return True


def check_thresholds(client, now=None):
    """Send the 80% / 100% warnings the first time the client crosses each
    one this month, per sold bucket (web conversations, WhatsApp
    conversations). Returns the (bucket, threshold) pairs fired on this
    call -- for the web bucket the bare threshold, for backwards
    compatibility: [80, 100, ('wa', 80)]. Cheap when nothing is due: one
    cache read per bucket, and at most one grouped COUNT. Dedupe is the
    cache key first (cache.add) and an AuditEvent 'allowance.N' /
    'allowance.wa.N' per month as the durable record."""
    ent = entitlements(client)
    included = ent.get('monthly_conversations')
    if ent['unit'] != 'conversations' or not included:
        return []
    start, end = month_bounds(now)
    label = timezone.localtime(start, BUSINESS_TZ).strftime('%Y-%m')
    status = None
    fired = []
    for bucket, prefix, _bucket_label in BUCKETS:
        try:
            if cache.get(threshold_key(client.pk, label, THRESHOLDS[-1], bucket)):
                continue
        except Exception:
            pass
        if status is None:
            status = allowance_status(client, start, ent=ent)
        figures = status if bucket == 'web' else status[bucket]
        if not figures['included']:
            continue     # bucket not sold on this plan (or capacity 0)
        for threshold in THRESHOLDS:
            if figures['used'] * 100 < figures['included'] * threshold:
                continue
            key = threshold_key(client.pk, label, threshold, bucket)
            try:
                fresh = cache.add(key, 1, _THRESHOLD_TTL)
            except Exception:
                fresh = True
            if not fresh:
                continue
            if not _record_threshold(client, f'{prefix}.{threshold}', label,
                                     f'{figures["used"]}/{figures["included"]}'):
                continue
            _notify_threshold(client, threshold, status, bucket=bucket)
            fired.append(threshold if bucket == 'web' else (bucket, threshold))
    return fired


def after_turn(client, conversation, first_turn):
    """Hook for landing.turns.run_turn: best-effort, never raises, only
    does work when a conversation just started (thresholds can only be
    crossed by a new conversation)."""
    if client is None or conversation is None or not first_turn:
        return
    try:
        check_thresholds(client)
    except Exception:
        logger.exception('Allowance threshold check failed for %s', client.slug)


def _notify_threshold(client, threshold, status, bucket='web'):
    views = _plans()
    label = status['month_label']
    figures = status if bucket == 'web' else status[bucket]
    bucket_label = dict((b, name) for b, _p, name in BUCKETS)[bucket]
    context = {
        'client': client, 'threshold': threshold, 'status': status,
        'bucket': bucket, 'bucket_label': bucket_label,
        'used': figures['used'], 'included': figures['included'], 'pct': figures['pct'],
        'block_size': figures['block_size'], 'block_price': figures['block_price'],
        'projection': figures['projection'], 'month_label': label,
        'annual': getattr(_subscription_of(client), 'period', '') == 'annual',
        'dashboard_url': settings.SITE_URL + reverse('reports'),
        'billing_url': settings.SITE_URL + reverse('billing'),
    }
    if threshold >= 100:
        subject = (f'Tu agente ya usó las {figures["included"]} {bucket_label} '
                   f'de tu plan este mes')
    else:
        subject = f'Tu agente va por el {figures["pct"]}% de las {bucket_label} de tu plan'
    if client.notify_email:
        views._send_html_email(subject, [client.notify_email],
                               'landing/emails/allowance_warning.html',
                               'landing/emails/allowance_warning.txt', context)
    ops = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    if ops:
        views._send_plain_email(
            f'[Capacidad] {client.slug} al {figures["pct"]}% de {bucket_label} ({label})',
            [ops],
            (f'{client.name} ({client.slug}, plan {status["plan"]} escalón {status["step"]}) '
             f'lleva {figures["used"]} de {figures["included"]} {bucket_label} en {label} '
             f'({figures["pct"]}%). Proyección: {figures["projection"]}. '
             f'Exceso hasta ahora: {figures["overage_blocks"]} bloque(s) de '
             f'{figures["block_size"]} a {figures["block_price"]} + IVU '
             f'= {price_label(figures["overage_cents"])}. El servicio sigue; a fin de mes '
             f'bill_overage lo factura.'))
