"""
Phase 2 — the centralized client platform (dashboard modules).

Every view here follows the same two rules that keep tenants separated and the
product understandable:
  1. SCOPING: querysets are filtered by membership BEFORE any pk lookup — a
     cross-tenant id 404s, it never leaks.
  2. ROLES (M-17): staff = DOMINIO operator (sees everything); org_admin =
     manages their organization (knowledge, users, settings); agent = works
     leads and conversations only.
"""
import csv
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

import anthropic
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST

from . import agent, analytics, entitlements, ingest, knowledge, publishing, turns
from . import leads as lead_service
from .health import alert as ops_alert, quiet as quiet_5xx
from . import usage as usage_ledger
from .models import (
    AvailabilityRule, Booking, Client, ContactSubmission, Conversation,
    KnowledgeSource, Location, Membership, StaffContact, UsageEvent,
)
from .views import (
    MAX_CHAT_BODY_BYTES, _audit, _back, _client_ip, _member_client_ids, _rate_limited,
    _send_plain_email, _tenant_action_cap_hit, booking_notify_visitor, leads_for,
    staff_required,
)

logger = logging.getLogger(__name__)


# ============================================================
# Scoping + role helpers (M-17)
# ============================================================

def clients_for(user):
    """Organizations this user may operate on."""
    if user.is_staff:
        return Client.objects.all()
    ids = _member_client_ids(user)
    return Client.objects.filter(id__in=ids) if ids else Client.objects.none()


def is_org_admin(user, client=None):
    """True for staff always; for members only with the org_admin role (in the
    given client, or in ANY of their clients when client is None — used to show
    or hide whole nav sections)."""
    if user.is_staff:
        return True
    qs = Membership.objects.filter(user=user, role='org_admin')
    if client is not None:
        qs = qs.filter(client=client)
    return qs.exists()


def conversations_for(user):
    """Conversations this user may read. Phase E: an employee's chat with
    the internal assistant (channel='internal') is readable only by staff and
    by an org_admin OF THAT CLIENT — never by an agent, never by another
    employee (employee memberships are not a dashboard scope at all)."""
    qs = Conversation.objects.select_related('client', 'lead', 'user')
    if user.is_staff:
        return qs
    ids = _member_client_ids(user)
    if not ids:
        return qs.none()
    admin_ids = list(Membership.objects.filter(
        user=user, role='org_admin', client_id__in=ids).values_list('client_id', flat=True))
    return qs.filter(client_id__in=ids).filter(
        ~Q(channel='internal') | Q(client_id__in=admin_ids))


def bookings_for(user):
    qs = Booking.objects.select_related('client', 'lead', 'location')
    if user.is_staff:
        return qs
    ids = _member_client_ids(user)
    return qs.filter(client_id__in=ids) if ids else qs.none()


def _scoped_client_or_404(user, pk):
    return get_object_or_404(clients_for(user), pk=pk)


# ============================================================
# M-02 / M-16 — Conversations console
# ============================================================

# Whitelisted manual transitions (the agent/tools drive 'active'/'escalated').
ALLOWED_TRANSITIONS = {
    'followup': {'active', 'escalated'},
    'closed_resolved': {'active', 'escalated', 'followup'},
    'closed_unresolved': {'active', 'escalated', 'followup'},
    'blocked': {'active', 'escalated', 'followup'},
    'active': {'blocked'},  # unblock
}


INBOX_CHANNEL_LABELS = {'web': 'Web', 'whatsapp': 'WhatsApp', 'internal': 'Asistente interno'}
INBOX_DETAIL_LIST = 40


def _inbox_filter(request):
    """The inbox querystring applied to the scoped conversations. Returns
    (qs, state, channel, client_slug, q); unknown values filter nothing."""
    qs = conversations_for(request.user)
    state = request.GET.get('state', '')
    channel = request.GET.get('channel', '')
    client_slug = (request.GET.get('client') or '').strip()[:60]
    q = (request.GET.get('q') or '').strip()
    if state in dict(Conversation.STATE_CHOICES):
        qs = qs.filter(state=state)
    else:
        state = ''
    if channel in dict(Conversation.CHANNEL_CHOICES):
        qs = qs.filter(channel=channel)
    else:
        channel = ''
    if client_slug:
        # Scoped first: a foreign slug filters to nothing, never widens.
        qs = qs.filter(client__slug=client_slug)
    if q:
        qs = qs.filter(
            Q(chat_messages__content__icontains=q)
            | Q(lead__name__icontains=q) | Q(lead__email__icontains=q)
        ).distinct()
    qs = qs.annotate(n_messages=Count('chat_messages', distinct=True))
    return qs, state, channel, client_slug, q


def _inbox_chip_groups(request, state, channel, client_slug):
    """State and channel chips with counts over the scope (and client
    filter), each an href that toggles it. Two grouped queries."""
    from .views import _filter_href
    base = conversations_for(request.user)
    if client_slug:
        base = base.filter(client__slug=client_slug)
    by_state = dict(base.order_by().values_list('state').annotate(n=Count('pk')))
    by_channel = dict(base.order_by().values_list('channel').annotate(n=Count('pk')))

    def chip(param, value, label, count, current):
        active = current == value
        return {'label': label, 'count': count, 'active': active,
                'href': _filter_href(request, **{param: '' if active else value})}

    return [
        {'key': 'state', 'label': 'Estado', 'chips': [
            chip('state', key, label, by_state.get(key, 0), state)
            for key, label in Conversation.STATE_CHOICES
            if by_state.get(key) or state == key]},
        {'key': 'channel', 'label': 'Canal', 'chips': [
            chip('channel', key, INBOX_CHANNEL_LABELS.get(key, label), by_channel.get(key, 0), channel)
            for key, label in Conversation.CHANNEL_CHOICES
            if by_channel.get(key) or channel == key]},
    ]


def _inbox_qs(state, channel, client_slug, q):
    """'?state=..&q=..' to carry the inbox filters into the detail links."""
    from urllib.parse import urlencode
    params = {k: v for k, v in (('state', state), ('channel', channel),
                                ('client', client_slug), ('q', q)) if v}
    return '?' + urlencode(params) if params else ''


@login_required
def conversations(request):
    """Inbox: scoped list with state, channel (web|whatsapp|internal),
    client (slug within scope) and text filters. Each row carries
    `contact_label` ("WhatsApp · termina en 1234") for rows without a lead."""
    qs, state, channel, client_slug, q = _inbox_filter(request)
    scoped = conversations_for(request.user)
    stats = {
        'total': scoped.count(),
        'escalated': scoped.filter(state='escalated').count(),
        'with_lead': scoped.filter(lead__isnull=False).count(),
        'whatsapp': scoped.filter(channel='whatsapp').count(),
    }
    return render(request, 'landing/dashboard_conversations.html', {
        'active_tab': 'conversations',
        'conversations': qs[:200], 'stats': stats,
        'state_choices': Conversation.STATE_CHOICES,
        'channel_choices': Conversation.CHANNEL_CHOICES,
        'cur_state': state, 'cur_channel': channel, 'cur_client': client_slug, 'q': q,
        'client_options': list(clients_for(request.user).order_by('name')
                               .values_list('slug', 'name')),
        'inbox_groups': _inbox_chip_groups(request, state, channel, client_slug),
        'multi_client': clients_for(request.user).count() > 1,
        'inbox_qs': _inbox_qs(state, channel, client_slug, q),
        'filters_active': bool(state or channel or client_slug or q),
    })


@login_required
def conversation_detail(request, pk):
    conv = get_object_or_404(conversations_for(request.user), pk=pk)
    # Reading a transcript is access to visitor PII: audit it, but once per
    # user+conversation per hour so reloads don't flood the trail.
    try:
        first_view = cache.add(f'auditview:conv:{request.user.pk}:{conv.pk}', 1, 3600)
    except Exception:  # best-effort like _audit: a cache hiccup must not 500 the page
        first_view = True
    if first_view:
        _audit(request, 'conversation.view', client=conv.client,
               target=f'conv:{conv.pk}')
    survey = getattr(conv, 'survey', None)
    clinic = bool(conv.client.clinic_mode)
    lead = conv.lead
    booking = (lead_service.lead_bookings(lead).order_by('-start').first()
               if lead is not None else None)
    # The list stays beside the thread (same filters as the inbox).
    list_qs, state, channel, client_slug, q = _inbox_filter(request)
    return render(request, 'landing/dashboard_conversation_detail.html', {
        'active_tab': 'conversations',
        'inbox_items': list(list_qs[:INBOX_DETAIL_LIST]),
        'multi_client': clients_for(request.user).count() > 1,
        'inbox_qs': _inbox_qs(state, channel, client_slug, q),
        'filters_active': bool(state or channel or client_slug or q),
        'conv': conv,
        'msgs': conv.chat_messages.order_by('position'),
        'survey': survey,
        'state_choices': Conversation.STATE_CHOICES,
        'allowed': sorted(new for new, olds in ALLOWED_TRANSITIONS.items()
                          if conv.state in olds),
        'contact_label': conv.contact_label,
        # The side panel: everything known about this conversation besides
        # the transcript. Clinic clients: no topic / first question (free
        # text that may carry health details).
        'context': {
            'lead': lead,
            'booking': booking,
            'page_url': conv.page_url,
            'intent': conv.intent,
            'intent_label': analytics.INTENT_LABELS.get(conv.intent, ''),
            'topic': '' if clinic else conv.topic,
            'first_question': '' if clinic else conv.first_question,
            'csat': survey,
            'escalation_reason': conv.escalation_reason,
            'contact_label': conv.contact_label,
            'channel': conv.channel,
            'channel_label': conv.get_channel_display(),
            'clinic': clinic,
            'state_label': conv.get_state_display(),
        },
    })


@login_required
@require_POST
def conversation_state(request, pk):
    """Manual state change from the console — closing requires classifying the
    result (that's why 'closed' isn't one state but two)."""
    conv = get_object_or_404(conversations_for(request.user), pk=pk)
    new_state = request.POST.get('state', '')
    if new_state not in ALLOWED_TRANSITIONS or conv.state not in ALLOWED_TRANSITIONS[new_state]:
        messages.error(request, 'Transición de estado no permitida.')
        return redirect('conversation_detail', pk=pk)
    Conversation.objects.filter(pk=conv.pk).update(state=new_state)
    _audit(request, 'conversation.state', client=conv.client,
           target=f'conv:{conv.pk}', result=new_state)
    messages.success(request, 'Estado actualizado.')
    return redirect('conversation_detail', pk=pk)


# ============================================================
# M-05 — Bookings console + availability rules
# ============================================================

BOOKING_TRANSITIONS = {
    'confirmed': {'pending'},
    'cancelled': {'pending', 'confirmed'},
    'completed': {'confirmed'},
    'no_show': {'confirmed'},
}


@login_required
def bookings_list(request):
    qs = bookings_for(request.user)
    my_clients = clients_for(request.user).filter(enable_bookings=True)
    # Phase F: the locations the user may see, and an optional ?location=<pk>
    # filter on the lists (scoped: a foreign pk simply filters nothing).
    locations = (Location.objects.filter(client__in=clients_for(request.user))
                 .select_related('client'))
    location_filter = None
    try:
        location_filter = locations.filter(pk=int(request.GET.get('location', ''))).first()
    except (TypeError, ValueError):
        pass
    if location_filter is not None:
        qs = qs.filter(location=location_filter)
    status_filter = request.GET.get('status', '')
    by_status = dict(qs.order_by().values_list('status').annotate(n=Count('pk')))
    if status_filter in dict(Booking.STATUS_CHOICES):
        qs = qs.filter(status=status_filter)
    else:
        status_filter = ''
    now = timezone.now()
    upcoming = qs.filter(start__gte=now).exclude(status='cancelled').order_by('start')[:100]
    past = qs.filter(start__lt=now).order_by('-start')[:50]
    week = _week_grid(qs, request.GET.get('week'))
    from .views import _filter_href
    week_urls = {'prev': _filter_href(request, week=week['prev']),
                 'next': _filter_href(request, week=week['next']),
                 'today': _filter_href(request, week='')}
    status_chips = [{'label': BOOKING_STATUS_ES.get(key, label), 'count': by_status.get(key, 0),
                     'active': status_filter == key,
                     'href': _filter_href(request, status='' if status_filter == key else key)}
                    for key, label in Booking.STATUS_CHOICES]
    can_manage_rules = is_org_admin(request.user)
    rules = (AvailabilityRule.objects.filter(client__in=my_clients)
             .select_related('client', 'location'))
    # How many locations each plan allows (the save view enforces it), and
    # which clients exceed it after a downgrade: the extras keep working,
    # the page only says no more can be added.
    location_limits = {c.pk: int(entitlements.entitlements(c)['locations'] or 0)
                       for c in my_clients}
    location_counts = {}
    for loc in locations:
        location_counts[loc.client_id] = location_counts.get(loc.client_id, 0) + 1
    location_over = [{'client': c, 'allowed': location_limits[c.pk],
                      'count': location_counts.get(c.pk, 0)}
                     for c in my_clients if location_counts.get(c.pk, 0) > location_limits[c.pk]]
    return render(request, 'landing/dashboard_bookings.html', {
        'active_tab': 'bookings',
        'upcoming': upcoming, 'past': past, 'rules': rules,
        'rule_clients': my_clients, 'can_manage_rules': can_manage_rules,
        'locations': locations, 'location_filter': location_filter,
        'location_limits': location_limits, 'location_over': location_over,
        'has_locations': locations.exists(),
        'weekdays': [(0, 'Lunes'), (1, 'Martes'), (2, 'Miércoles'), (3, 'Jueves'),
                     (4, 'Viernes'), (5, 'Sábado'), (6, 'Domingo')],
        'transitions': BOOKING_TRANSITIONS,
        'cur_status': status_filter,
        'booking_status_choices': Booking.STATUS_CHOICES,
        'week': week,
        'week_urls': week_urls,
        'status_chips': status_chips,
        'filters_active': bool(status_filter or location_filter),
    })


BOOKING_STATUS_ES = {
    'pending': 'Pendiente', 'confirmed': 'Confirmada', 'cancelled': 'Cancelada',
    'completed': 'Completada', 'no_show': 'No llegó',
}
WEEK_DAY_LABELS = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']
WEEK_MIN_HOUR, WEEK_MAX_HOUR = 8, 18


def _week_grid(qs, week_param):
    """The weekly calendar: Monday to Sunday (Puerto Rico) of the week that
    contains ?week=YYYY-MM-DD (default: today), with every booking of `qs`
    (already scoped and filtered) placed as a block. One query.

    Returns {'start', 'end' (dates), 'prev', 'next', 'today' (ISO strings
    for links), 'is_current', 'start_hour', 'end_hour', 'hours' ([int]),
    'count', 'days': [{'date', 'label', 'is_today', 'bookings': [{'booking',
    'start_local', 'end_local', 'top_pct', 'height_pct', 'status',
    'status_label', 'status_class'}]}]}. top/height are % of the visible
    day column, so the template needs no arithmetic."""
    tz = analytics.BUSINESS_TZ
    today = timezone.localtime(timezone.now(), tz).date()
    anchor = today
    if week_param:
        try:
            anchor = date.fromisoformat(week_param[:10])
        except ValueError:
            anchor = today
    monday = anchor - timedelta(days=anchor.weekday())
    week_start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    week_end = week_start + timedelta(days=7)
    rows = list(qs.filter(start__gte=week_start, start__lt=week_end).order_by('start', 'pk'))
    start_hour, end_hour = WEEK_MIN_HOUR, WEEK_MAX_HOUR
    placed = []
    for b in rows:
        local = timezone.localtime(b.start, tz)
        local_end = local + timedelta(minutes=b.duration_minutes or 30)
        start_hour = min(start_hour, local.hour)
        end_hour = max(end_hour, local_end.hour + (1 if local_end.minute else 0))
        placed.append((b, local, local_end))
    end_hour = min(24, max(end_hour, start_hour + 1))
    span = (end_hour - start_hour) * 60
    days = []
    for i in range(7):
        day = monday + timedelta(days=i)
        days.append({'date': day, 'label': f'{WEEK_DAY_LABELS[i]} {day.day}',
                     'is_today': day == today, 'bookings': []})
    for b, local, local_end in placed:
        offset = (local.hour - start_hour) * 60 + local.minute
        length = max(15, (b.duration_minutes or 30))
        days[local.weekday()]['bookings'].append({
            'booking': b, 'start_local': local, 'end_local': local_end,
            'top_pct': round(offset * 100 / span, 2),
            'height_pct': round(min(length, span - offset) * 100 / span, 2),
            'status': b.status, 'status_label': BOOKING_STATUS_ES.get(b.status, b.status),
            'status_class': f'is-{b.status}',
        })
    return {
        'start': monday, 'end': monday + timedelta(days=6),
        'prev': (monday - timedelta(days=7)).isoformat(),
        'next': (monday + timedelta(days=7)).isoformat(),
        'today': today.isoformat(), 'is_current': monday <= today <= monday + timedelta(days=6),
        'start_hour': start_hour, 'end_hour': end_hour,
        'hours': list(range(start_hour, end_hour)), 'count': len(rows), 'days': days,
    }


BOOKING_STATUS_LABELS_ES = {'pending': 'pendiente', 'confirmed': 'confirmada',
                            'cancelled': 'cancelada', 'completed': 'completada',
                            'no_show': 'no llegó'}


@login_required
@require_POST
def booking_status(request, pk):
    booking = get_object_or_404(bookings_for(request.user), pk=pk)
    new_status = request.POST.get('status', '')
    if new_status not in BOOKING_TRANSITIONS or booking.status not in BOOKING_TRANSITIONS[new_status]:
        messages.error(request, 'Transición de cita no permitida.')
        return _back(request, 'bookings')
    Booking.objects.filter(pk=booking.pk).update(status=new_status)
    _audit(request, 'booking.status', client=booking.client,
           target=f'booking:{booking.pk}', result=new_status)
    # AC M-05: the visitor hears about confirmations and cancellations. By
    # email when the booking has one; otherwise by the channel the booking
    # came from (Phase G: a WhatsApp template, else SMS), never an email to
    # an empty address. Phase F: with the location, so a multi-site visitor
    # knows where to go. One selector for every booking notice.
    if new_status in ('confirmed', 'cancelled'):
        booking_notify_visitor(booking, new_status)
    label = BOOKING_STATUS_LABELS_ES.get(new_status, new_status)
    messages.success(request, f'Cita marcada como {label}.')
    # Back to the same week and filters the user was looking at.
    return _back(request, 'bookings')


@login_required
@require_POST
def availability_add(request):
    if not is_org_admin(request.user):
        messages.error(request, 'Solo un administrador puede configurar horarios.')
        return redirect('bookings')
    client = get_object_or_404(clients_for(request.user), pk=request.POST.get('client'))
    if not is_org_admin(request.user, client):
        messages.error(request, 'Solo un administrador puede configurar horarios.')
        return redirect('bookings')
    try:
        weekday = int(request.POST.get('weekday', ''))
        open_t = request.POST.get('open_time', '')
        close_t = request.POST.get('close_time', '')
        if not (0 <= weekday <= 6) or not open_t or not close_t or open_t >= close_t:
            raise ValueError
    except (TypeError, ValueError):
        messages.error(request, 'Revisa el día y las horas (apertura antes del cierre).')
        return redirect('bookings')
    # Phase F: optional location; blank = the whole business. It must be one
    # of THIS client's locations (a foreign pk is rejected, never attached).
    location = None
    loc_pk = (request.POST.get('location') or '').strip()
    if loc_pk:
        location = Location.objects.filter(client=client, pk=loc_pk).first() if loc_pk.isdigit() else None
        if location is None:
            messages.error(request, 'Ese local no existe.')
            return redirect('bookings')
    AvailabilityRule.objects.update_or_create(
        client=client, location=location, weekday=weekday,
        defaults={'open_time': open_t, 'close_time': close_t})
    _audit(request, 'availability.set', client=client,
           target=f'weekday:{weekday}' + (f'|location:{location.pk}' if location else ''),
           result=f'{open_t}-{close_t}')
    messages.success(request, 'Horario guardado.')
    return redirect('bookings')


@login_required
@require_POST
def availability_delete(request, pk):
    rule = get_object_or_404(
        AvailabilityRule.objects.filter(client__in=clients_for(request.user)), pk=pk)
    if not is_org_admin(request.user, rule.client):
        messages.error(request, 'Solo un administrador puede configurar horarios.')
        return redirect('bookings')
    _audit(request, 'availability.delete', client=rule.client, target=str(rule))
    rule.delete()
    messages.success(request, 'Horario eliminado.')
    return redirect('bookings')


# ============================================================
# Phase F — Locations (multi-site businesses)
# ============================================================

@login_required
@require_POST
def location_save(request):
    """Create or update (POST `pk`) a Location of the org_admin's client.
    The plan decides how many a tenant may have (entitlements['locations']);
    a refused creation is audited as location.denied. A downgrade never
    deletes existing locations: it only stops new ones."""
    from .phone import normalize_phone
    client = _admin_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar locales.')
        return redirect('bookings')
    name = ' '.join((request.POST.get('name') or '').split())[:80]
    address = ' '.join((request.POST.get('address') or '').split())[:200]
    phone = normalize_phone(request.POST.get('phone') or '')
    is_default = request.POST.get('is_default') == 'on'
    pk = (request.POST.get('pk') or '').strip()
    existing = (get_object_or_404(Location.objects.filter(client=client), pk=pk)
                if pk.isdigit() else None)
    if not name:
        messages.error(request, 'Escribe el nombre del local (ej. Bayamón).')
        return redirect('bookings')
    if phone is None:
        messages.error(request, 'Escribe un teléfono válido de 10 dígitos, ej. (787) 123-4567.')
        return redirect('bookings')
    result = name
    try:
        with transaction.atomic():
            # Lock the client row first (a real lock on Postgres, a no-op on
            # SQLite, same as user_invite): the duplicate-name and plan-limit
            # checks and the insert happen under it, so two concurrent saves
            # cannot both pass the count. The unique constraint on (client,
            # name) is the backstop for the name.
            Client.objects.select_for_update().get(pk=client.pk)
            dup = client.locations.filter(name__iexact=name)
            if existing is not None:
                dup = dup.exclude(pk=existing.pk)
            if dup.exists():
                messages.error(request, f'Ya existe un local llamado "{name}".')
                return redirect('bookings')
            if existing is None:
                allowed = int(entitlements.entitlements(client)['locations'] or 0)
                count = client.locations.count()
                if count >= allowed:
                    _audit(request, 'location.denied', client=client, target=name,
                           result=f'limit:{allowed}')
                    messages.error(request, (f'Tu plan incluye {allowed} local(es). Para '
                                             'añadir más, escríbenos y ajustamos tu plan.'))
                    return redirect('bookings')
            if is_default:
                client.locations.filter(is_default=True).update(is_default=False)
            if existing is None:
                location = Location.objects.create(
                    client=client, name=name, address=address, phone=phone or '',
                    is_default=is_default, order=count)
                if count == 0:
                    # The FIRST location inherits the business-wide bookings
                    # still to come: from now on the handler assigns every new
                    # booking to a location, and a slot must not be held twice
                    # (once with location NULL, once with the location).
                    moved = Booking.objects.filter(
                        client=client, location__isnull=True, start__gte=timezone.now(),
                        status__in=('pending', 'confirmed')).update(location=location)
                    if moved:
                        result = f'{name}|citas_reasignadas:{moved}'
            else:
                location = existing
                Location.objects.filter(pk=existing.pk).update(
                    name=name, address=address, phone=phone or '', is_default=is_default)
    except IntegrityError:
        messages.error(request, 'No se pudo guardar el local; vuelve a intentarlo.')
        return redirect('bookings')
    _audit(request, 'location.add' if existing is None else 'location.update',
           client=client, target=f'location:{location.pk}', result=result)
    messages.success(request, 'Local guardado.')
    return redirect('bookings')


@login_required
@require_POST
def location_delete(request, pk):
    """Remove a Location. Refused while it still has pending/confirmed future
    bookings (the visitor was told where to go); past bookings keep their
    row with location=NULL (SET_NULL). Its own hours go with it (CASCADE)."""
    location = get_object_or_404(
        Location.objects.filter(client__in=clients_for(request.user)), pk=pk)
    if not is_org_admin(request.user, location.client):
        messages.error(request, 'Solo un administrador puede gestionar locales.')
        return redirect('bookings')
    with transaction.atomic():
        # Lock the row so a booking landing between the check and the delete
        # cannot be orphaned (Postgres; no-op on SQLite).
        location = Location.objects.select_for_update().get(pk=location.pk)
        if location.bookings.filter(status__in=('pending', 'confirmed'),
                                    start__gte=timezone.now()).exists():
            messages.error(request, (f'"{location.name}" tiene citas pendientes o confirmadas; '
                                     'cancélalas o complétalas antes de quitarlo.'))
            return redirect('bookings')
        _audit(request, 'location.remove', client=location.client,
               target=f'location:{location.pk}', result=location.name)
        location.delete()
    messages.success(request, 'Local eliminado.')
    return redirect('bookings')


# ============================================================
# M-03 — Reports / analytics per organization
# ============================================================

# Volume fields a tenant may see. 'cost' (DOMINIO's AI spend) is deliberately
# absent: the tenant context is built from this whitelist, never from the
# full analytics row, so a cost figure cannot reach the template by accident.
TENANT_VOLUME_KEYS = ('conversations', 'escalated', 'leads', 'chat_leads', 'bookings',
                      'messages', 'csat_avg', 'csat_n')
WEEKDAY_LABELS = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']


def _operator_block(start, end):
    """Staff-only: real AI spend across every client in [start, end) (demo
    included). Fixed number of queries. Never called for a tenant."""
    ledger = usage_ledger.rollup_by_client(start, end)
    conv_rows = {
        r['client_id']: r for r in
        Conversation.objects.filter(started_at__gte=start, started_at__lt=end)
        .order_by().values('client_id')
        .annotate(n=Count('id'), leads=Count('lead', distinct=True))}
    ids = (set(conv_rows) | set(ledger)) - {None}
    names = dict(Client.objects.filter(pk__in=ids).values_list('pk', 'name'))
    names[None] = 'Demo público / sin cliente'
    per_client = []
    for cid in ids | ({None} & set(ledger)):
        used = ledger.get(cid, {})
        conv = conv_rows.get(cid, {})
        per_client.append({
            'client__name': names.get(cid, f'#{cid}'),
            'n': conv.get('n', 0), 'leads': conv.get('leads', 0),
            'messages': used.get('messages', 0), 'tokens': used.get('tokens', 0),
            'cost': used.get('cost', Decimal(0)).quantize(Decimal('0.0001')),
        })
    per_client.sort(key=lambda r: (-r['cost'], -r['n'], r['client__name']))
    return {
        'est_cost': sum((row['cost'] for row in ledger.values()),
                        Decimal(0)).quantize(Decimal('0.0001')),
        'tokens': sum(row['tokens'] for row in ledger.values()),
        # The ledger only exists from its deploy on; older traffic has no cost row.
        'ledger_since': (UsageEvent.objects.order_by('created_at')
                         .values_list('created_at', flat=True).first()),
        'per_client': per_client[:50],
    }


def _hours_display(rules):
    """[(label, open, close)] for the business hours actually used."""
    hours = rules or analytics.DEFAULT_HOURS
    return [(WEEKDAY_LABELS[wd], hours[wd][0], hours[wd][1]) for wd in sorted(hours)]


@login_required
def reports(request):
    """Tenant dashboard for ONE agent: allowance in messages, value for the
    price paid, and what visitors did. Never any DOMINIO cost figure for a
    tenant (the staff-only operator block is computed only for staff).

    Scoping: the agent is resolved through clients_for(user); a ?client= slug
    outside that set falls back to the user's own first agent."""
    user = request.user
    period, start, end = analytics.period_bounds(request.GET.get('period'))
    scope = clients_for(user)
    options = list(scope.order_by('name').values_list('slug', 'name'))
    wanted = (request.GET.get('client') or '').strip()
    slugs = {slug for slug, _name in options}
    slug = wanted if wanted in slugs else (options[0][0] if options else None)
    client = scope.select_related('subscription').filter(slug=slug).first() if slug else None

    context = {
        'active_tab': 'reports',
        'client_obj': client, 'client_options': options,
        'period': period, 'periods': analytics.PERIODS,
        'period_label': analytics.PERIOD_LABELS[period],
        'start': start, 'end': end,
        'operator': _operator_block(start, end) if user.is_staff else None,
    }
    if client is None:
        return render(request, 'landing/dashboard_reports.html', context)

    ids = [client.pk]
    row = analytics.volume(ids, start, end).get(client.pk) or analytics.empty_volume()
    stats = {key: row[key] for key in TENANT_VOLUME_KEYS}
    rules = analytics.business_hours(ids).get(client.pk)
    after = analytics.after_hours(ids, start, end, rules={client.pk: rules} if rules else {}
                                  ).get(client.pk, 0)
    convs = stats['conversations']
    stats.update({
        'conversion': round(stats['chat_leads'] * 100 / convs) if convs else 0,
        'after_hours': after,
        'after_hours_pct': round(after * 100 / convs) if convs else 0,
    })
    month_start, month_end = usage_ledger.month_bounds()
    daily = analytics.daily_series(client.pk, days=30)  # has 'cost': only bars leave here

    def pages(qs, date_field):
        return analytics.hbars(list(
            qs.filter(client_id=client.pk, **{f'{date_field}__gte': start,
                                                f'{date_field}__lt': end})
            .exclude(page_url='').order_by().values('page_url')
            .annotate(n=Count('id')).order_by('-n', 'page_url')[:10]))

    context.update({
        'stats': stats,
        'allowance': analytics.allowance(client, month_start, month_end),
        # Phase E: the employees' assistant is reported on its own line, never
        # mixed into the visitor figures (period and calendar month).
        'internal_conversations': Conversation.objects.filter(
            client_id=client.pk, channel='internal',
            started_at__gte=start, started_at__lt=end).count(),
        'internal_month': Conversation.objects.filter(
            client_id=client.pk, channel='internal',
            started_at__gte=month_start, started_at__lt=month_end).count(),
        'value': analytics.value_metrics(client, start, end, vol=row, after=after),
        'month_start': month_start,
        'msg_bars': analytics.svg_bars(daily, 'messages'),
        'conv_bars': analytics.svg_bars(daily, 'conversations'),
        'daily_messages': sum(d['messages'] for d in daily),
        'daily_conversations': sum(d['conversations'] for d in daily),
        'intents': analytics.hbars(analytics.top_intents(ids, start, end)),
        # Clinic clients: stored topics never reach the template, even ones
        # tagged before clinic mode was switched on.
        'topics': ([] if client.clinic_mode
                   else analytics.hbars(analytics.top_topics(ids, start, end, n=10))),
        'hours': _hours_display(rules), 'custom_hours': bool(rules),
        'lead_pages': pages(ContactSubmission.objects, 'created_at'),
        'conv_pages': pages(Conversation.objects, 'started_at'),
        # Escalation reasons = the raw material for the monthly tuning cycle (M-19).
        'unanswered': list(
            Conversation.objects.filter(client_id=client.pk, state='escalated',
                                        started_at__gte=start, started_at__lt=end)
            .exclude(escalation_reason='').order_by('-started_at')
            .values_list('escalation_reason', flat=True)[:20]),
    })
    context.update(_crm_report_block(client, start, end))
    return render(request, 'landing/dashboard_reports.html', context)


KPI_BAD_WHEN_UP = {'escalated'}  # for these, a rise is bad news (delta in red)


def _decorate_kpis(kpis, client):
    """Display fields for the KPI cards, shared by the home and the reports
    page. Adds to each item: 'display' (value + suffix), 'delta_text' ("+12 %",
    "+2 pts", "Nuevo", "Sin cambios"), 'delta_dir' (up|down|flat), 'delta_good'
    (good|bad|neutral) and 'href' (the list behind the figure, or ''). Existing
    keys are untouched."""
    slug = client.slug
    q = f'?client={slug}'
    hrefs = {
        'conversations': reverse('conversations') + q,
        'leads': reverse('dashboard') + q,
        'escalated': reverse('conversations') + f'{q}&state=escalated',
        'bookings': reverse('bookings') + q if client.enable_bookings else '',
    }
    for k in kpis:
        key, cur, prev, pct = k['key'], k['value'], k['prev'], k['pct']
        if cur is None:
            k['display'] = '—'
        elif key == 'csat':
            k['display'] = f'{cur:.1f}{k["suffix"]}'
        elif isinstance(cur, int):
            k['display'] = f'{cur:,}{" " if k["suffix"] == "%" else ""}{k["suffix"]}'
        else:
            k['display'] = f'{cur}{k["suffix"]}'
        direction = k['direction']
        if key == 'conversion':
            diff = int(cur or 0) - int(prev or 0)
            text = f'{diff:+d} pts' if diff else 'Sin cambios'
            direction = 'up' if diff > 0 else 'down' if diff < 0 else 'flat'
        elif key == 'csat':
            if cur is None:
                text, direction = 'Sin respuestas', 'flat'
            elif prev is None:
                text, direction = 'Nuevo', 'up'
            else:
                diff = round(cur - prev, 1)
                text = f'{diff:+.1f}' if diff else 'Sin cambios'
                direction = 'up' if diff > 0 else 'down' if diff < 0 else 'flat'
        elif pct is None:
            text = 'Nuevo' if cur else 'Sin cambios'
            direction = 'up' if cur else 'flat'
        elif pct == 0:
            text, direction = 'Sin cambios', 'flat'
        else:
            text = f'{pct:+d} %'
        k['delta_text'], k['delta_dir'] = text, direction
        if direction == 'flat':
            k['delta_good'] = 'neutral'
        elif (direction == 'up') != (key in KPI_BAD_WHEN_UP):
            k['delta_good'] = 'good'
        else:
            k['delta_good'] = 'bad'
        k['href'] = hrefs.get(key, '')
    return kpis


def _decorate_charts(block):
    """Geometry and text the chart templates need on top of analytics.svg_*:
    channel_chart gets 'hits' (one hover column per day with its tooltip),
    'legend' (totals per channel) and 'summary'; peak_chart cells get 'tip'
    (only the non-empty ones) and the chart a 'summary'. Mutates and returns
    `block`."""
    chart = block['channel_chart']
    plot, rows, n = chart['plot'], chart['rows'], len(chart['rows'])
    step = plot['w'] / (n - 1) if n > 1 else plot['w']
    classes = {'web': 'web', 'whatsapp': 'wa'}
    hits = []
    for i, row in enumerate(rows):
        cx = plot['x'] + (i * plot['w'] / (n - 1) if n > 1 else plot['w'] / 2)
        left = max(plot['x'], cx - step / 2)
        right = min(plot['x'] + plot['w'], cx + step / 2)
        parts = ', '.join(f'{col} {v}' for col, v in zip(chart['columns'], row['cells']))
        day = f"{analytics.WEEKDAYS_ES[row['day'].weekday()]} {row['day']:%d/%m}"
        hits.append({'x': f'{left:.1f}', 'w': f'{right - left:.1f}',
                     'tip': f"{day}: {parts}. Total {row['total']}"})
    chart['hits'] = hits
    chart['legend'] = [
        {'label': col, 'cls': classes.get(layer['key'], 'web'),
         'value': sum(row['cells'][j] for row in rows)}
        for j, (col, layer) in enumerate(zip(chart['columns'], chart['layers']))]
    if rows and not chart['empty']:
        best = max(rows, key=lambda r: r['total'])
        chart['summary'] = (
            f"{chart['total']} conversaciones en {n} días: "
            + ', '.join(f"{leg['label']} {leg['value']}" for leg in chart['legend'])
            + f". El día con más: {best['day']:%d/%m} ({best['total']}).")
    else:
        chart['summary'] = 'Sin conversaciones en estos días.'
    heat = block['peak_chart']
    for cell in heat['cells']:
        cell['tip'] = (f"{cell['day']} {cell['hour']}: {cell['n']} "
                       f"conversaci{'ón' if cell['n'] == 1 else 'ones'}") if cell['n'] else ''
    top = block['peak'].get('top') or []
    heat['summary'] = ('Los momentos con más conversaciones: '
                       + ', '.join(f"{t['day']} {t['hour']} ({t['n']})" for t in top) + '.'
                       if top else 'Sin conversaciones en este periodo.')
    return block


def _crm_report_block(client, start, end):
    """CRM figures shared by the home and the reports page for one client
    and period. Tenant-safe (no usage ledger, no cost). Keys: kpis
    (analytics.kpis), channel_series (daily_by_channel, 30 d),
    channel_chart (svg_area), funnel + funnel_chart, peak + peak_chart,
    csat (csat_distribution), resolution (resolution_rate), prev_start,
    prev_end."""
    prev_start, prev_end = analytics.previous_period(start, end)
    cur = analytics.kpi_counts(client, start, end)
    prev = analytics.kpi_counts(client, prev_start, prev_end)
    channel_series = analytics.daily_by_channel(client, days=30)
    fun = analytics.funnel(client, start, end)
    stages = fun['stages'] if fun['has_bookings_stage'] else fun['stages'][:2]
    peak = analytics.peak_hours(client, start, end)
    return _decorate_charts({
        'kpis': _decorate_kpis(analytics.kpis(client, start, end, cur=cur, prev=prev), client),
        'kpi_counts': cur,
        'prev_start': prev_start, 'prev_end': prev_end,
        'channel_series': channel_series,
        'channel_chart': analytics.svg_area(channel_series, ['web', 'whatsapp']),
        'funnel': fun,
        'funnel_chart': analytics.svg_funnel(stages),
        'peak': peak,
        'peak_chart': analytics.svg_heatmap(peak),
        'csat': analytics.csat_distribution(client, start, end, clinic=client.clinic_mode),
        'resolution': analytics.resolution_rate(client, start, end),
    })


# ============================================================
# CRM home (/dashboard/): attention band, KPIs, charts, agent status
# ============================================================

@login_required
def home(request):
    """The dashboard home for ONE agent: what needs attention now, the
    period KPIs with their delta, the 30-day channel series, the funnel,
    peak hours, topics and the agent's status. Same scoping as reports:
    ?client=<slug> within clients_for(user), else the first one. Employees
    never get here (EmployeeScopeMiddleware sends them to the assistant)."""
    user = request.user
    period, start, end = analytics.period_bounds(request.GET.get('period'))
    scope = clients_for(user)
    options = list(scope.order_by('name').values_list('slug', 'name'))
    wanted = (request.GET.get('client') or '').strip()
    slugs = {slug for slug, _name in options}
    slug = wanted if wanted in slugs else (options[0][0] if options else None)
    client = scope.select_related('subscription').filter(slug=slug).first() if slug else None
    context = {
        'active_tab': 'home',
        'client_obj': client, 'client_options': options,
        'period': period, 'periods': analytics.PERIODS,
        'period_label': analytics.PERIOD_LABELS[period],
        'start': start, 'end': end,
    }
    if client is None:
        return render(request, 'landing/dashboard_home.html', context)

    block = _crm_report_block(client, start, end)
    # 30-day sparklines under the KPIs (one grouped query each).
    series = block['channel_series']
    leads_daily = analytics.daily_counts(client, 30, 'leads')
    bookings_daily = analytics.daily_counts(client, 30, 'bookings')
    escalated_daily = analytics.daily_counts(client, 30, 'escalated')
    conv_values = [d['total'] for d in series]
    lead_values = [d['value'] for d in leads_daily]
    sparks = {
        'conversations': analytics.sparkline(conv_values),
        'leads': analytics.sparkline(lead_values),
        'bookings': analytics.sparkline([d['value'] for d in bookings_daily]),
        'escalated': analytics.sparkline([d['value'] for d in escalated_daily]),
        'conversion': analytics.sparkline([
            round(lv * 100 / cv) if cv else 0 for lv, cv in zip(lead_values, conv_values)]),
        'csat': None,
    }
    for kpi in block['kpis']:
        kpi['spark'] = sparks.get(kpi['key'])

    ids = [client.pk]
    month_start, month_end = usage_ledger.month_bounds()
    now = timezone.now()
    seen = client.widget_last_seen_at
    ent = entitlements.entitlements(client)
    agent_status = {
        'setup_status': client.setup_status,
        'setup_label': client.get_setup_status_display(),
        'is_live': client.setup_status == 'live' and client.is_active,
        'is_active': client.is_active,
        'widget_last_seen_at': seen,
        'seen_recently': bool(seen and seen >= now - timedelta(hours=24)),
        'live_at': client.live_at,
        'website_url': client.website_url,
        'profile': client.agent_profile,
        'profile_label': client.get_agent_profile_display(),
        'channels': sorted(ent.get('channels', ()) or ()),
        'enable_bookings': client.enable_bookings,
        'clinic_mode': client.clinic_mode,
        'plan': ent.get('plan', ''),
    }
    context.update(block)
    context.update({
        'clinic': client.clinic_mode,
        'attention': analytics.attention(client, user),
        'follow_ups': analytics.follow_up_lists(client),
        'intents': analytics.hbars(analytics.top_intents(ids, start, end)),
        # Clinic clients: stored topics never reach the template.
        'topics': ([] if client.clinic_mode
                   else analytics.hbars(analytics.top_topics(ids, start, end, n=10))),
        'agent': agent_status,
        'allowance': analytics.allowance(client, month_start, month_end),
        'month_start': month_start,
        'status_labels': ContactSubmission.STATUS_LABELS_ES,
    })
    return render(request, 'landing/dashboard_home.html', context)


# ============================================================
# M-07 — CSV export (audited, scoped, Excel-friendly)
# ============================================================

def _csv_safe(value):
    """Neutralise spreadsheet formula injection. A visitor who names himself
    `=HYPERLINK("http://evil")` would otherwise become a live formula the
    moment the tenant opens leads.csv in Excel. The leading apostrophe makes
    Excel/LibreOffice treat the cell as text."""
    text = '' if value is None else str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r', '\n') else text


@login_required
def export_leads_csv(request):
    # Same querystring as the leads board/list (channel, assigned, tag,
    # follow-up, stale... on top of source/status/q): what the user sees
    # filtered is what the CSV contains. Tenant scope comes first.
    from .views import _lead_filters
    base_qs, filters = _lead_filters(request, leads_for(request.user))
    qs = base_qs.filter(status=filters['status']) if filters['status'] else base_qs
    qs = qs.select_related('assigned_to').prefetch_related('tags').distinct()
    source, status, q = filters['source'], filters['status'], filters['q']

    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = 'attachment; filename="leads.csv"'
    resp.write('﻿')  # BOM so Excel opens accents correctly
    writer = csv.writer(resp)
    writer.writerow(['Nombre', 'Email', 'Teléfono', 'Compañía', 'Servicio',
                     'Fuente', 'Estado', 'Página', 'Mensaje', 'Fecha',
                     'Asignado a', 'Etiquetas', 'Próximo seguimiento'])
    tz = analytics.BUSINESS_TZ
    for lead in qs[:5000]:
        follow_up = ''
        if lead.follow_up_at and not lead.follow_up_done_at:
            follow_up = timezone.localtime(lead.follow_up_at, tz).strftime('%Y-%m-%d %H:%M')
        writer.writerow([_csv_safe(v) for v in (
            lead.name, lead.email, lead.phone, lead.company,
            lead.get_service_display(), lead.get_source_display(),
            lead.get_status_display(), lead.page_url,
            lead.message[:1000], lead.created_at.strftime('%Y-%m-%d %H:%M'),
            lead.assigned_to.get_username() if lead.assigned_to_id else '',
            ', '.join(sorted(t.name for t in lead.tags.all())), follow_up)])
    applied = ' '.join(f'{k}={v}' for k, v in filters.items() if v and k != 'q')
    _audit(request, 'leads.export', target=f'{qs.count()} filas',
           result=f'filtros {applied} q={q[:40]}'.strip()[:200])
    return resp


# ============================================================
# M-15 / M-19 — Knowledge base + test-before-publish console
# ============================================================

KNOWLEDGE_CLIENT_MESSAGE = ('El conocimiento de tu agente lo gestiona DOMINIO. Escríbenos a '
                            'hola@dominiopr.com si quieres añadir o corregir algo.')


def _admin_client(request):
    """Resolve the organization an org_admin (or staff) is operating on:
    ?client=<slug> / POST client, else the first one. The gate for users,
    contacts and locations, which tenants keep managing themselves."""
    qs = clients_for(request.user)
    slug = request.GET.get('client') or request.POST.get('client') or ''
    client = qs.filter(slug=slug).first() if slug else qs.first()
    if client is None or not is_org_admin(request.user, client):
        return None
    return client


def _knowledge_client(request):
    """Resolve which organization's knowledge is being managed. Staff only
    (Phase I): DOMINIO loads and maintains every tenant's knowledge; tenants
    (org_admin, agent, employee) never see these views. Staff pick the tenant
    with ?client=<slug> / POST client; without it, the first one."""
    if not request.user.is_staff:
        return None
    qs = clients_for(request.user)
    slug = request.GET.get('client') or request.POST.get('client') or ''
    return qs.filter(slug=slug).first() if slug else qs.first()


def _knowledge_denied(request):
    """A non-staff user reached a knowledge URL: back to their dashboard with
    the one message that matters to them (who to write to)."""
    if request.user.is_staff:
        messages.error(request, 'No hay ningún agente con ese identificador (o ninguno configurado).')
    else:
        messages.info(request, KNOWLEDGE_CLIENT_MESSAGE)
    return redirect('dashboard')


def _knowledge_context(request, client, **extra):
    sources = list(client.knowledge_sources.exclude(status='archived').select_related('replaces'))
    archived = list(client.knowledge_sources.filter(status='archived'))
    # Phase 6: past versions per row, and which live source has a pending draft.
    history = publishing.history_for(sources, archived)
    # (a queued/processing copy counts: the live row must not offer a 2nd edit)
    pending_for = {s.replaces_id for s in sources
                   if s.status in ('draft', 'error', 'pending', 'processing') and s.replaces_id}
    for s in sources:
        s.history = history.get(s.pk, [])
        s.has_open_draft = s.pk in pending_for
    context = {
        'active_tab': 'knowledge',
        'client_obj': client, 'sources': sources,
        'archived': archived,
        'all_clients': clients_for(request.user),
        'kind_choices': [c for c in KnowledgeSource.KIND_CHOICES
                         if c[0] in KnowledgeSource.MANUAL_KINDS],
        'audience_choices': KnowledgeSource.AUDIENCE_CHOICES,
        'has_internal': 'internal' in entitlements.entitlements(client).get('channels', ()),
        'scan_url': ingest.default_scan_url(client),
        'scan_running': any(s.kind == 'site' and s.in_progress for s in sources),
        'any_in_progress': any(s.in_progress for s in sources),
        'upload_max_mb': ingest.upload_max_bytes() // (1024 * 1024),
        'test_question': '', 'test_answer': None,
        'can_publish': publishing.can_publish(request.user, client),
        'sizes': publishing.size_report(client),
    }
    context.update(extra)
    return context


def _knowledge_redirect(client):
    return redirect(reverse('knowledge') + f'?client={client.slug}')


@login_required
def knowledge_list(request):
    client = _knowledge_client(request)
    if client is None:
        return _knowledge_denied(request)
    return render(request, 'landing/dashboard_knowledge.html',
                  _knowledge_context(request, client))


@login_required
@require_POST
@_rate_limited('kscan', limit=5, window=3600, html='knowledge')
def knowledge_scan(request):
    """Phase 8: queue a crawl of the tenant's own website. The crawl and the
    model calls run in the process_knowledge cron; the result lands as a draft."""
    client = _knowledge_client(request)
    if client is None:
        return _knowledge_denied(request)
    try:
        url = ingest.validate_scan(client, request.POST.get('url'),
                                   any_host=request.user.is_staff)
    except ValueError as exc:
        messages.error(request, str(exc))
        return _knowledge_redirect(client)
    if _tenant_action_cap_hit('ingest', client):
        messages.error(request, 'Llegaste al límite de escaneos y archivos de hoy para este '
                                'agente. Intenta de nuevo mañana.')
        return _knowledge_redirect(client)
    try:
        source = ingest.enqueue_site_scan(client, url)
    except ValueError as exc:
        messages.error(request, str(exc))
        return _knowledge_redirect(client)
    _audit(request, 'knowledge.scan', client=client, target=url, result=f'source:{source.pk}')
    messages.success(request, 'Escaneo en cola. Se procesa en segundo plano: recarga en unos '
                              'minutos y el resultado aparecerá como borrador para revisarlo.')
    return _knowledge_redirect(client)


@csrf_exempt
@login_required
def knowledge_upload(request):
    """Phase 8: upload a document; its text is extracted here (bounded, in
    memory) and summarized later by the cron. The file is never stored.

    The upload handlers must be replaced before anything reads request.POST,
    and CsrfViewMiddleware reads it, so CSRF is enforced on the inner view
    (the documented Django pattern) instead of by the middleware.

    Staff only, decided BEFORE the body is parsed: a tenant login must not be
    able to make a worker buffer DOMINIO_INGEST_UPLOAD_MAX_MB in memory just
    to be told no."""
    if not request.user.is_staff:
        return _knowledge_denied(request)
    handler = ingest.BoundedMemoryUploadHandler(request)
    request.upload_handlers = [handler]
    request.ingest_upload_handler = handler
    return _knowledge_upload(request)


@csrf_protect
@require_POST
@_rate_limited('kupload', limit=10, window=3600, html='knowledge')
def _knowledge_upload(request):
    client = _knowledge_client(request)
    if client is None:
        return _knowledge_denied(request)
    handler = getattr(request, 'ingest_upload_handler', None)
    uploaded = request.FILES.get('file')
    if uploaded is None:
        if handler is not None and handler.too_large:
            messages.error(request, f'El archivo es demasiado grande '
                                    f'(máx. {ingest.upload_max_bytes() // (1024 * 1024)} MB).')
        else:
            messages.error(request, 'Escoge un archivo para subir.')
        return _knowledge_redirect(client)
    if _tenant_action_cap_hit('ingest', client):
        messages.error(request, 'Llegaste al límite de escaneos y archivos de hoy para este '
                                'agente. Intenta de nuevo mañana.')
        return _knowledge_redirect(client)
    try:
        source = ingest.enqueue_file(client, uploaded,
                                     audience=request.POST.get('audience', 'public'))
    except ValueError as exc:
        messages.error(request, str(exc))
        return _knowledge_redirect(client)
    finally:
        uploaded.close()
    _audit(request, 'knowledge.upload', client=client, target=source.title,
           result=f'source:{source.pk}')
    messages.success(request, f'"{source.title}" se leyó bien y quedó en cola. Se procesa en '
                              'segundo plano: recarga en unos minutos.')
    return _knowledge_redirect(client)


def _process_as_draft(source):
    """Extract text/FAQ into fragments without ever going live (Phase 6).
    Never for kind 'url': a tenant-controlled URL is never fetched inside a
    web request; it is queued (ingest.enqueue_url) for process_knowledge."""
    return knowledge.process_source(source, success_status='draft')


def _revision_in_progress(live):
    """A queued/processing draft copy of `live` exists: a second edit now
    would fork the lineage into two drafts."""
    return live is not None and live.revisions.filter(
        status__in=('pending', 'processing')).exists()


QUEUE_FULL_MESSAGE = 'Tienes demasiadas fuentes en cola. Espera a que se procesen.'
URL_QUEUED_MESSAGE = ('La página quedó en cola. Se lee en segundo plano: recarga en unos '
                      'minutos y aparecerá como borrador para probarla.')


@login_required
@require_POST
# Each save is a write (and a URL save queues a fetch): cap how often.
@_rate_limited('ksave', limit=10, window=300, html='knowledge')
def knowledge_save(request):
    """Create or update a source; it lands as DRAFT (or error) — activation is a
    separate, deliberate step after testing (RF-21).

    Phase 6: editing an ACTIVE source never touches it. The edit goes into a
    draft copy (replaces=<active>, reused if one is already open) and the live
    version keeps answering until that draft is activated."""
    client = _knowledge_client(request)
    if client is None:
        return _knowledge_denied(request)
    pk = request.POST.get('pk') or None
    source = (get_object_or_404(client.knowledge_sources, pk=pk) if pk
              else KnowledgeSource(client=client))
    if source.pk and source.in_progress:
        messages.error(request, 'Esa fuente se está procesando; espera a que termine.')
        return _knowledge_redirect(client)
    live = source if source.pk and source.status == 'active' else None
    if _revision_in_progress(live):
        messages.error(request, 'Esa fuente se está procesando; espera a que termine.')
        return _knowledge_redirect(client)
    if live is not None:
        source = publishing.open_draft_for(live)
    if source.kind not in KnowledgeSource.INGEST_KINDS:
        kind = request.POST.get('kind', 'text')
        # site/file only come from the ingest pipeline, never from this form.
        source.kind = kind if kind in KnowledgeSource.MANUAL_KINDS else 'text'
        source.origin = (request.POST.get('origin') or '').strip()[:500]
    # (an edited scan/upload keeps its kind and origin: only its text changes)
    source.title = (request.POST.get('title') or '').strip()[:160] or 'Sin título'
    source.content = (request.POST.get('content') or '').strip()[:60000]
    # Phase E: who the source is for. Unknown/missing value keeps what the
    # row had (new rows default to 'public'): a stale form can never turn a
    # public source internal or the other way round by accident.
    audience = request.POST.get('audience', '')
    if audience in dict(KnowledgeSource.AUDIENCE_CHOICES):
        source.audience = audience
    if source.kind == 'url' and ingest.queue_full(client):
        messages.error(request, QUEUE_FULL_MESSAGE)
        return _knowledge_redirect(client)
    source.status = 'pending' if source.kind == 'url' else 'draft'
    source.save()
    if source.kind == 'url':
        # Never fetched here: the process_knowledge cron reads the page and
        # leaves a draft (notifying DOMINIO for plans that can't publish).
        ingest.enqueue_url(source)
        messages.success(request, URL_QUEUED_MESSAGE + (
            f' La versión activa de "{live.title}" sigue publicada.' if live is not None else ''))
    elif _process_as_draft(source).status == 'error':
        # Text/FAQ: extract → fragments now; failures land visibly in 'error'.
        messages.error(request, f'La fuente quedó en error: {source.error}')
    else:
        if live is not None:
            messages.success(
                request, f'Tus cambios quedaron en un borrador. La versión activa de '
                         f'"{live.title}" sigue publicada hasta que actives el borrador.')
        else:
            messages.success(
                request, 'Fuente procesada y en borrador. Pruébala y luego actívala.')
    _audit(request, 'knowledge.save', client=client, target=source.title,
           result=source.status + (f' (nueva versión de #{live.pk})' if live else ''))
    return redirect(reverse('knowledge') + f'?client={client.slug}')


@login_required
@require_POST
# Activate/restore/reprocess are writes (and a reprocess queues a fetch).
@_rate_limited('kaction', limit=20, window=300, html='knowledge')
def knowledge_action(request, pk):
    client = _knowledge_client(request)
    if client is None:
        return _knowledge_denied(request)
    source = get_object_or_404(client.knowledge_sources, pk=pk)
    action = request.POST.get('action', '')
    # Phase 6: anything that changes what the live agent says is publishing;
    # since Phase I only staff reach this view, so the plan gate is gone.
    result = 'ok'
    audience = request.POST.get('audience', '')
    if action == 'audience' and audience in dict(KnowledgeSource.AUDIENCE_CHOICES):
        # Phase E: who the source is for, on any kind (scans and uploads have
        # no edit form). On a live source this changes what each assistant
        # says, hence the publish gate above. Never touches the text.
        KnowledgeSource.objects.filter(pk=source.pk).update(
            audience=audience, updated_at=timezone.now())
        messages.success(request, f'"{source.title}" ahora es: '
                                  f'{dict(KnowledgeSource.AUDIENCE_CHOICES)[audience]}.')
        result = audience
    elif action == 'audience':
        messages.error(request, 'Audiencia no válida.')
        result = 'no permitida'
    elif action in ('activate', 'restore'):
        try:
            retired = publishing.publish(source, restoring=(action == 'restore'))
        except publishing.PublishError as exc:
            messages.error(request, str(exc))
            result = f'rechazada: {exc}'
        else:
            verb = 'restaurada' if action == 'restore' else 'activa'
            note = ' La versión anterior quedó archivada.' if retired else ''
            messages.success(request, f'"{source.title}" está {verb} — el agente ya la usa.{note}')
            result = 'activa' + (f'; archivadas: {", ".join(retired)}' if retired else '')
    elif action == 'archive' and source.in_progress:
        messages.error(request, 'Esa fuente se está procesando. Espera a que termine o cancélalo.')
    elif action == 'archive':
        archive = {'status': 'archived', 'updated_at': timezone.now()}
        if source.kind == 'file':
            archive['content'] = ''  # the uploaded text is not kept once retired
        # Conditional update: the cron may have claimed it since we loaded it.
        if KnowledgeSource.objects.filter(pk=source.pk).exclude(
                status__in=('pending', 'processing')).update(**archive):
            messages.success(request, f'"{source.title}" archivada.')
        else:
            messages.error(request, 'Esa fuente se está procesando. Espera a que termine o cancélalo.')
    elif action == 'reprocess' and source.kind in KnowledgeSource.INGEST_KINDS:
        return _retry_ingest(request, client, source)
    elif action == 'reprocess' and source.status == 'active' and _revision_in_progress(source):
        messages.error(request, 'Esa fuente se está procesando; espera a que termine.')
        result = 'no permitida: en proceso'
    elif action == 'reprocess' and source.kind == 'url' and not source.in_progress \
            and ingest.queue_full(client):
        messages.error(request, QUEUE_FULL_MESSAGE)
        result = 'rechazada: cola llena'
    elif action == 'reprocess' and not source.in_progress:
        # Re-fetching a LIVE source writes into its draft copy, like an edit.
        target = source
        if source.status == 'active':
            target = publishing.open_draft_for(source)
            if target.pk is None:
                target.status = 'pending' if target.kind == 'url' else 'draft'
                target.save()
        live_note = ' La versión activa sigue publicada.' if target is not source else ''
        if target.kind == 'url':
            # Queued, never fetched inside the request (see knowledge_save).
            ingest.enqueue_url(target)
            messages.success(request, URL_QUEUED_MESSAGE + live_note)
            result = 'en cola'
        else:
            _process_as_draft(target)
            messages.success(request, f'"{target.title}" reprocesada '
                                      f'({target.get_status_display()}).' + live_note)
    elif action == 'delete' and source.status in ('pending', 'draft', 'error', 'archived'):
        publishing.delete_source(source)
        messages.success(request, 'Fuente eliminada.')
    else:
        messages.error(request, 'Acción no permitida para el estado de esta fuente.')
        result = 'no permitida'
    _audit(request, f'knowledge.{action}', client=client, target=source.title, result=result)
    return redirect(reverse('knowledge') + f'?client={client.slug}')


def _retry_ingest(request, client, source):
    """Re-queue a failed scan/upload. Only from 'error': a draft or active
    source is never pulled out from under the agent (re-scan = a new scan)."""
    if source.status != 'error' or (source.kind == 'file' and not source.content.strip()):
        messages.error(request, 'Solo se puede reintentar una fuente con error. '
                                'Para actualizarla, escanea o sube de nuevo.')
        return _knowledge_redirect(client)
    if source.kind == 'site':
        # Same checks as a new scan: allowed_origins may have changed since the
        # source was queued, and the host may no longer resolve to a public IP.
        try:
            ingest.validate_scan(client, source.origin, any_host=request.user.is_staff)
        except ValueError as exc:
            messages.error(request, str(exc))
            return _knowledge_redirect(client)
    if _tenant_action_cap_hit('ingest', client):
        messages.error(request, 'Llegaste al límite de escaneos y archivos de hoy para este '
                                'agente. Intenta de nuevo mañana.')
        return _knowledge_redirect(client)
    try:
        with transaction.atomic():
            KnowledgeSource.objects.filter(pk=source.pk, status='error').update(
                status='pending', error='', progress='En cola…', attempts=0,
                updated_at=timezone.now())
    except IntegrityError:  # a scan got queued between the check and here
        messages.error(request, 'Ya hay un escaneo de tu sitio en curso. Espera a que termine.')
        return _knowledge_redirect(client)
    _audit(request, 'knowledge.retry', client=client, target=source.title)
    messages.success(request, f'"{source.title}" volvió a la cola. Recarga en unos minutos.')
    return _knowledge_redirect(client)


@login_required
@require_POST
# 'knowledge' = fallback target when there's no Referer (this path is POST-only).
@_rate_limited('ktest', limit=10, window=300, html='knowledge')
def knowledge_test(request):
    """RF-21/M-19: ask a test question against the compiled knowledge (optionally
    including ONE draft source) before anything goes live. Never persists.
    Each test is a paid model call: per-IP throttle plus a per-tenant daily cap."""
    client = _knowledge_client(request)
    if client is None:
        return _knowledge_denied(request)
    question = (request.POST.get('question') or '').strip()[:2000]
    draft_pk = request.POST.get('draft') or None
    # Phase E: test as a visitor (public knowledge, web rules) or as an
    # employee (internal knowledge, internal rules). Never both at once.
    as_internal = request.POST.get('as') == 'internal'
    test_answer = None
    if question and _tenant_action_cap_hit('ktest', client):
        messages.error(request, 'Llegaste al límite de pruebas de hoy para este agente. '
                                'Intenta de nuevo mañana.')
    elif question and as_internal and not client.compiled_internal_prompt(
            include_draft_pk=draft_pk).strip():
        # Same short-circuit as assistant_api: without internal documents the
        # model would answer from nothing (never from DOMINIO's own copy).
        test_answer = NO_INTERNAL_DOCS_MESSAGE
    elif question:
        if as_internal:
            prompt = client.compiled_internal_prompt(include_draft_pk=draft_pk)
            # Same kwargs run_turn builds for the assistant (clinic + profile),
            # so the console tests exactly what the employees get.
            extra = dict(agent.clinic_kwargs(client), profile='internal')
        else:
            prompt = client.compiled_prompt(include_draft_pk=draft_pk)
            extra = agent.clinic_kwargs(client)
        try:
            test_answer, test_usage = agent.answer(
                [{'role': 'user', 'content': question}],
                business_prompt=prompt, handlers={},
                language=client.primary_language, **extra)
        except agent.AgentNotConfigured:
            test_answer = '(El agente no está configurado — falta ANTHROPIC_API_KEY.)'
        except anthropic.APIError:
            logger.exception('Knowledge test call failed')
            test_answer = '(Error consultando el modelo — trata de nuevo.)'
        else:
            usage_ledger.record('knowledge_test', test_usage, client=client)
    return render(request, 'landing/dashboard_knowledge.html', _knowledge_context(
        request, client, test_question=question, test_answer=test_answer,
        tested_draft=str(draft_pk or ''), tested_as=('internal' if as_internal else 'web')))


# ============================================================
# M-17 — Users & roles per organization
# ============================================================

def _seats_used(client):
    """Internal seats in use: active users whose membership is not a
    dashboard role (entitlements.NON_SEAT_ROLES), i.e. employees. Same rule
    the month-end seat overage uses, restricted to active logins."""
    return (client.members.exclude(role__in=entitlements.NON_SEAT_ROLES)
            .filter(user__is_active=True).count())


@login_required
def users_list(request):
    client = _admin_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar usuarios.')
        return redirect('dashboard')
    members = client.members.select_related('user')
    ent = entitlements.entitlements(client)
    return render(request, 'landing/dashboard_users.html', {
        'active_tab': 'users',
        'client_obj': client, 'members': members,
        'all_clients': clients_for(request.user),
        'role_choices': Membership.ROLE_CHOICES,
        # Phase E: internal seats (employee accounts) the plan includes.
        'has_internal': 'internal' in ent.get('channels', ()),
        'seats_included': int(ent.get('seats') or 0),
        'seats_used': _seats_used(client),
        # Phase D: routing of the receptionist's messages.
        'staff_contacts': client.staff_contacts.select_related('location'),
        'contact_channels': StaffContact.CHANNEL_CHOICES,
        # Phase F: a contact may serve one location only.
        'locations': client.locations.all(),
    })


@login_required
@require_POST
def staff_contact_add(request):
    """Phase D: add a StaffContact (who gets the messages for a department or
    by name). org_admin of the client only; the phone is normalized server-side."""
    from .phone import normalize_phone
    client = _admin_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar contactos.')
        return redirect('dashboard')
    back = redirect(reverse('dash_users') + f'?client={client.slug}')
    name = (request.POST.get('name') or '').strip()[:120]
    department = (request.POST.get('department') or '').strip()[:80]
    email = (request.POST.get('email') or '').strip().lower()[:254]
    phone = normalize_phone(request.POST.get('phone') or '')
    channel = request.POST.get('channel', 'email')
    channel = channel if channel in dict(StaffContact.CHANNEL_CHOICES) else 'email'
    if not name:
        messages.error(request, 'Escribe el nombre del contacto.')
        return back
    if email:
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, 'Escribe un email válido.')
            return back
    if phone is None:
        messages.error(request, 'Escribe un teléfono válido de 10 dígitos, ej. (787) 123-4567.')
        return back
    if not email and not phone:
        messages.error(request, 'El contacto necesita un email o un teléfono.')
        return back
    if channel != 'email' and not phone:
        messages.error(request, 'Para SMS o WhatsApp el contacto necesita un teléfono.')
        return back
    if client.staff_contacts.count() >= 50:
        messages.error(request, 'Máximo 50 contactos por agente.')
        return back
    # Phase F: optional location, which must be this client's.
    location = None
    loc_pk = (request.POST.get('location') or '').strip()
    if loc_pk:
        location = Location.objects.filter(client=client, pk=loc_pk).first() if loc_pk.isdigit() else None
        if location is None:
            messages.error(request, 'Ese local no existe.')
            return back
    is_default = request.POST.get('is_default') == 'on'
    with transaction.atomic():
        if is_default:
            client.staff_contacts.filter(is_default=True).update(is_default=False)
        contact = StaffContact.objects.create(
            client=client, name=name, department=department, email=email,
            phone=phone or '', channel=channel, is_default=is_default,
            location=location, order=client.staff_contacts.count())
    _audit(request, 'contact.add', client=client, target=f'contact:{contact.pk}',
           result=department or 'por nombre')
    messages.success(request, 'Contacto guardado.')
    return back


@login_required
@require_POST
def staff_contact_delete(request, pk):
    client = _admin_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar contactos.')
        return redirect('dashboard')
    contact = get_object_or_404(StaffContact.objects.filter(client=client), pk=pk)
    contact.delete()
    _audit(request, 'contact.remove', client=client, target=f'contact:{pk}')
    messages.success(request, 'Contacto eliminado.')
    return redirect(reverse('dash_users') + f'?client={client.slug}')


@login_required
@require_POST
def user_invite(request):
    from .views import _provision_client_login  # late import: avoids cycle at load
    client = _admin_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar usuarios.')
        return redirect('dashboard')
    email = (request.POST.get('email') or '').strip().lower()[:254]
    role = request.POST.get('role', 'agent')
    role = role if role in dict(Membership.ROLE_CHOICES) else 'agent'
    try:
        validate_email(email)
    except ValidationError:
        messages.error(request, 'Escribe un email válido.')
        return redirect(reverse('dash_users') + f'?client={client.slug}')
    # The seat count and the membership it guards are decided inside ONE
    # transaction holding the client row lock: two concurrent employee
    # invites would otherwise both read "4 of 5" and both get in. The lock is
    # a no-op on SQLite and a real row lock on Postgres. The email goes out
    # after the commit.
    with transaction.atomic():
        if role == 'employee':
            Client.objects.select_for_update().get(pk=client.pk)
            # Phase E: an employee is an internal seat. Only plans that include
            # the internal assistant have seats, and only as many as sold.
            ent = entitlements.entitlements(client)
            seats = int(ent.get('seats') or 0)
            if 'internal' not in ent.get('channels', ()) or not seats:
                messages.error(request, 'Tu plan no incluye el asistente interno.')
                _audit(request, 'user.invite', client=client, target=email,
                       result='denegado: plan sin asistente interno')
                return redirect(reverse('dash_users') + f'?client={client.slug}')
            used = _seats_used(client)
            if used >= seats:
                messages.error(
                    request, f'Llegaste al límite de {seats} asiento{"s" if seats != 1 else ""} '
                             f'del asistente interno ({used} en uso). Quita un empleado o '
                             'amplía tu plan.')
                _audit(request, 'user.invite', client=client, target=email,
                       result=f'denegado: asientos {used}/{seats}')
                return redirect(reverse('dash_users') + f'?client={client.slug}')

        User = get_user_model()
        existing = User.objects.filter(username=email).first()
        if existing and (existing.is_staff or existing.is_superuser):
            messages.error(request, 'Ese email pertenece a una cuenta de staff.')
            return redirect(reverse('dash_users') + f'?client={client.slug}')

        # An invite never edits an existing member: re-inviting an org_admin as
        # 'agent' would otherwise silently demote them (possibly the last admin).
        if Membership.objects.filter(client=client, user__username=email).exists():
            messages.error(request, 'Esa persona ya es miembro de este agente.')
            return redirect(reverse('dash_users') + f'?client={client.slug}')

        # Reuse the provisioning path: a new account gets a one-time link to
        # create its password; an existing one (member of another agent) gets
        # no link.
        original_notify = client.notify_email
        client.notify_email = email  # provision against the invited address
        username, setup_url = _provision_client_login(client)
        client.notify_email = original_notify
        if username:
            # Only the membership just created above (checked absent before).
            Membership.objects.filter(user__username=username, client=client).update(role=role)
    if username:
        body = (f'Te invitaron al dashboard de {client.name} en DOMINIO Chat 24/7.\n\n'
                f'Entra en: {request.build_absolute_uri(reverse("dashboard"))}\n'
                f'Usuario: {username}\n')
        if setup_url:
            body += ('Crea tu contraseña aquí (enlace de un solo uso, vence en 7 días):\n'
                     f'{setup_url}')
        else:
            body += ('Usa tu contraseña existente (tu cuenta ya administraba otro agente). '
                     'Si no la recuerdas, usa "¿Olvidaste tu contraseña?" en la página de entrada.')
        _send_plain_email(f'Acceso al dashboard — {client.name}', [email], body)
        _audit(request, 'user.invite', client=client, target=email, result=role)
        messages.success(request, f'Invitación enviada a {email} ({role}).')
    else:
        messages.error(request, 'No se pudo crear el acceso.')
    return redirect(reverse('dash_users') + f'?client={client.slug}')


@login_required
@require_POST
def user_remove(request, pk):
    client = _admin_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar usuarios.')
        return redirect('dashboard')
    membership = get_object_or_404(client.members, pk=pk)
    if membership.user_id == request.user.pk:
        messages.error(request, 'No puedes quitarte a ti mismo.')
        return redirect(reverse('dash_users') + f'?client={client.slug}')
    with transaction.atomic():
        # Lock the client's admin rows so two concurrent removals can't both
        # pass the check and leave the organization with no org_admin.
        admin_ids = list(client.members.select_for_update()
                         .filter(role='org_admin').values_list('pk', flat=True))
        if membership.role == 'org_admin' and admin_ids == [membership.pk]:
            messages.error(request, 'No puedes quitar al último administrador de este agente.')
            return redirect(reverse('dash_users') + f'?client={client.slug}')
        user = membership.user
        _audit(request, 'user.remove', client=client, target=user.username)
        membership.delete()
        # A login with no membership left has nothing to open: deactivate it
        # (sessions stop authenticating, password reset skips it) instead of
        # leaving a live account behind. Never deleted -- its audit trail and
        # authored notes keep pointing at it -- and staff is never touched.
        # A later invite or purchase reactivates it (_provision_client_login).
        deactivated = (not user.is_staff and not user.is_superuser
                       and not Membership.objects.filter(user=user).exists())
        if deactivated:
            type(user).objects.filter(pk=user.pk).update(is_active=False)
            _audit(request, 'user.deactivate', client=client, target=user.username,
                   result='sin membresías')
    if deactivated:
        messages.success(request, 'Acceso eliminado. No tenía otros accesos, así que su '
                                  'cuenta quedó desactivada.')
    else:
        messages.success(request, 'Acceso eliminado (la cuenta sigue existiendo).')
    return redirect(reverse('dash_users') + f'?client={client.slug}')


# ============================================================
# Phase E — Internal assistant for employees
# ============================================================

# Messages one login may send to the assistant per hour (abuse/cost bound).
ASSISTANT_USER_HOURLY_LIMIT = 60
# Max characters of one question (same bound the core stores).
ASSISTANT_MAX_CHARS = turns.MAX_MESSAGE_CHARS
NO_INTERNAL_MESSAGE = 'Tu plan no incluye el asistente interno.'
NO_INTERNAL_DOCS_MESSAGE = ('Todavía no hay documentos internos cargados. Pide a tu '
                            'administrador que suba el manual o las políticas en Conocimiento.')


def _assistant_client(request, slug=''):
    """The client whose internal assistant this user is opening: one of the
    clients where they hold an INTERNAL_ROLES membership (`slug` picks among
    several); staff may pick any client by slug, else their own memberships.
    Scope is applied BEFORE the slug lookup: a foreign slug yields None.

    Without a slug, the first client (by pk) whose plan includes the
    assistant wins: the nav shows the link when ANY membership has it, so
    landing on a lower-plan client first would be a 403 for no reason."""
    user = request.user
    slug = (slug or '').strip()[:60]
    ids = list(Membership.objects.filter(user=user, role__in=Membership.INTERNAL_ROLES)
               .values_list('client_id', flat=True))
    qs = Client.objects.filter(id__in=ids) if ids else Client.objects.none()
    if user.is_staff:
        # Staff open any client's assistant: by slug, or (no slug) the first
        # one whose plan includes it, instead of a dead end in the factory.
        qs = Client.objects.all()
    qs = qs.filter(is_active=True).select_related('subscription')
    if slug:
        return qs.filter(slug=slug).first()
    candidates = list(qs.order_by('pk'))  # a login holds a handful of memberships
    for candidate in candidates:
        if _internal_enabled(candidate):
            return candidate
    return candidates[0] if candidates else None


def _internal_enabled(client):
    return 'internal' in entitlements.entitlements(client).get('channels', ())


def _assistant_external_id(user, now=None):
    """One conversation per employee per local day: the usage unit of the
    assistant. Never the username (external_id is not PII)."""
    day = timezone.localtime(now or timezone.now()).date()
    return f'user:{user.pk}:{day:%Y-%m-%d}'


def _counter_hit(key, limit, window):
    """Fixed-window counter on the shared cache: True once `limit` hits
    happened inside `window` seconds (the hit is counted otherwise)."""
    used = cache.get(key, 0)
    if used >= limit:
        return True
    if used == 0:
        cache.set(key, 1, window)
    else:
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, window)
    return False


@login_required
def assistant_page(request):
    """Phase E: the employees' chat with the internal assistant. Any member
    of the client (employee, agent, org_admin); staff for any client by
    slug. Renders today's transcript so a reload keeps the chat."""
    client = _assistant_client(request, request.GET.get('client'))
    if client is None:
        if request.user.is_staff:
            messages.info(request, 'Escoge un agente de la fábrica para abrir su asistente interno.')
            return redirect('clients_list')
        raise Http404
    enabled = _internal_enabled(client)
    conv = (Conversation.objects.filter(
        client=client, channel='internal',
        external_id=_assistant_external_id(request.user)).first() if enabled else None)
    # A login with several memberships picks the business; staff pick any.
    if request.user.is_staff:
        options = list(Client.objects.filter(is_active=True)
                       .order_by('name').values_list('slug', 'name'))
    else:
        options = list(Client.objects.filter(
            id__in=Membership.objects.filter(
                user=request.user, role__in=Membership.INTERNAL_ROLES)
            .values_list('client_id', flat=True), is_active=True)
            .order_by('name').values_list('slug', 'name'))
    return render(request, 'landing/dashboard_assistant.html', {
        'active_tab': 'asistente',
        'client_obj': client, 'enabled': enabled,
        'no_internal_message': NO_INTERNAL_MESSAGE,
        'client_options': options,
        'msgs': conv.chat_messages.order_by('position') if conv is not None else [],
        'max_chars': ASSISTANT_MAX_CHARS,
    }, status=200 if enabled else 403)


@require_POST
def assistant_api(request):
    """Phase E: one turn with the internal assistant. Session auth + CSRF
    (the middleware; this view is NOT csrf_exempt), no tools, no captcha.
    Body: {"message": "...", "client": "<slug>"?}. Replies {"reply": "..."}.

    Limits: body size, message length, 60 messages per user per hour and
    the tenant's daily_message_cap on its own counter (the public widget's
    quota and the assistant's never starve each other). The turn itself
    (transcript, ledger, allowance) is landing.turns.run_turn with the
    INTERNAL knowledge and the 'internal' profile."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Inicia sesión para usar el asistente.'}, status=401)
    if len(request.body) > MAX_CHAT_BODY_BYTES:
        return JsonResponse({'error': 'La solicitud es demasiado grande.'}, status=413)
    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'Solicitud inválida.'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'error': 'Solicitud inválida.'}, status=400)
    slug = payload.get('client') if isinstance(payload.get('client'), str) else ''
    client = _assistant_client(request, slug or request.GET.get('client'))
    if client is None:
        return JsonResponse({'error': 'No tienes acceso a este asistente.'}, status=403)
    if not _internal_enabled(client):
        return JsonResponse({'error': NO_INTERNAL_MESSAGE}, status=403)
    text = payload.get('message')
    text = text.strip() if isinstance(text, str) else ''
    if not text:
        return JsonResponse({'error': 'Escribe una pregunta.'}, status=400)
    if len(text) > ASSISTANT_MAX_CHARS:
        return JsonResponse(
            {'error': f'La pregunta es demasiado larga (máximo {ASSISTANT_MAX_CHARS} caracteres).'},
            status=400)
    prompt = client.compiled_internal_prompt()
    if not prompt.strip():
        # No internal knowledge at all (no header, no internal/both source):
        # say so instead of letting the model improvise (or, worse, fall
        # back to DOMINIO's own copy). No model call, no quota spent.
        return JsonResponse({'reply': NO_INTERNAL_DOCS_MESSAGE})
    if _counter_hit(f'rl:asistente:user:{request.user.pk}', ASSISTANT_USER_HOURLY_LIMIT, 3600):
        resp = JsonResponse({'error': 'Demasiadas preguntas seguidas. Espera unos minutos.'},
                            status=429)
        resp['Retry-After'] = '3600'
        return resp
    if client.daily_message_cap and _counter_hit(
            f'chat:internal:{client.slug}:{timezone.now():%Y%m%d}',
            client.daily_message_cap, 60 * 60 * 26):
        logger.warning('Internal assistant daily quota reached for %s', client.slug)
        return JsonResponse({'error': 'El asistente llegó a su límite de hoy. Intenta mañana.'},
                            status=503)

    conversation, _created = Conversation.objects.get_or_create(
        client=client, channel='internal',
        external_id=_assistant_external_id(request.user),
        defaults={'user': request.user, 'config_version': client.config_version()})
    try:
        result = turns.run_turn(
            client, conversation, text, channel='internal', handlers={},
            profile='internal', language=client.primary_language,
            business_prompt=prompt)
    except agent.AgentNotConfigured:
        return quiet_5xx(JsonResponse(
            {'error': 'El asistente no está configurado todavía.'}, status=503))
    except anthropic.APIError as exc:
        # One email an hour at most (landing.health.alert); every failure is
        # still a WARNING with the trace.
        ops_alert(logger, 'model:internal', 'Internal assistant call failed for %s (%s)',
                  client.slug, exc.__class__.__name__, exc_info=True)
        return quiet_5xx(JsonResponse({'error': 'Error consultando el modelo. Intenta de nuevo.'},
                                      status=502))
    return JsonResponse({'reply': result.reply, 'conversation': conversation.pk})


# ============================================================
# M-17 — Audit trail
# ============================================================

@login_required
def audit_list(request):
    from .models import AuditEvent
    if request.user.is_staff:
        qs = AuditEvent.objects.select_related('client', 'user')
    elif is_org_admin(request.user):
        # Only the businesses where this login is org_admin: an admin of A who
        # is an agent of B must not read B's trail (tags, statuses, emails).
        admin_of = Membership.objects.filter(
            user=request.user, role='org_admin').values('client_id')
        qs = AuditEvent.objects.filter(
            client_id__in=admin_of).select_related('client', 'user')
    else:
        messages.error(request, 'Solo un administrador puede ver la auditoría.')
        return redirect('dashboard')
    action = (request.GET.get('action') or '').strip()
    if action:
        qs = qs.filter(action__icontains=action)
    return render(request, 'landing/dashboard_audit.html', {
        'active_tab': 'audit',
        'events': qs[:300], 'cur_action': action,
    })
