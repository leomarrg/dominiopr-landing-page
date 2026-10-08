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
import logging
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
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST

from . import agent, analytics, ingest, knowledge, publishing
from . import usage as usage_ledger
from .models import (
    AvailabilityRule, Booking, Client, ContactSubmission, Conversation,
    KnowledgeSource, Membership, UsageEvent,
)
from .views import (
    _audit, _client_ip, _member_client_ids, _rate_limited, _send_plain_email,
    _tenant_action_cap_hit, leads_for, staff_required,
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
    qs = Conversation.objects.select_related('client', 'lead')
    if user.is_staff:
        return qs
    ids = _member_client_ids(user)
    return qs.filter(client_id__in=ids) if ids else qs.none()


def bookings_for(user):
    qs = Booking.objects.select_related('client', 'lead')
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


@login_required
def conversations(request):
    qs = conversations_for(request.user)
    state = request.GET.get('state', '')
    q = (request.GET.get('q') or '').strip()
    if state in dict(Conversation.STATE_CHOICES):
        qs = qs.filter(state=state)
    if q:
        qs = qs.filter(
            Q(chat_messages__content__icontains=q)
            | Q(lead__name__icontains=q) | Q(lead__email__icontains=q)
        ).distinct()
    qs = qs.annotate(n_messages=Count('chat_messages', distinct=True))
    scoped = conversations_for(request.user)
    stats = {
        'total': scoped.count(),
        'escalated': scoped.filter(state='escalated').count(),
        'with_lead': scoped.filter(lead__isnull=False).count(),
    }
    return render(request, 'landing/dashboard_conversations.html', {
        'active_tab': 'conversations',
        'conversations': qs[:200], 'stats': stats,
        'state_choices': Conversation.STATE_CHOICES,
        'cur_state': state, 'q': q,
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
    return render(request, 'landing/dashboard_conversation_detail.html', {
        'active_tab': 'conversations',
        'conv': conv,
        'msgs': conv.chat_messages.order_by('position'),
        'survey': getattr(conv, 'survey', None),
        'state_choices': Conversation.STATE_CHOICES,
        'allowed': sorted(new for new, olds in ALLOWED_TRANSITIONS.items()
                          if conv.state in olds),
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
    now = timezone.now()
    upcoming = qs.filter(start__gte=now).exclude(status='cancelled').order_by('start')[:100]
    past = qs.filter(start__lt=now).order_by('-start')[:50]
    my_clients = clients_for(request.user).filter(enable_bookings=True)
    can_manage_rules = is_org_admin(request.user)
    rules = AvailabilityRule.objects.filter(client__in=my_clients).select_related('client')
    return render(request, 'landing/dashboard_bookings.html', {
        'active_tab': 'bookings',
        'upcoming': upcoming, 'past': past, 'rules': rules,
        'rule_clients': my_clients, 'can_manage_rules': can_manage_rules,
        'weekdays': [(0, 'Lunes'), (1, 'Martes'), (2, 'Miércoles'), (3, 'Jueves'),
                     (4, 'Viernes'), (5, 'Sábado'), (6, 'Domingo')],
        'transitions': BOOKING_TRANSITIONS,
    })


@login_required
@require_POST
def booking_status(request, pk):
    booking = get_object_or_404(bookings_for(request.user), pk=pk)
    new_status = request.POST.get('status', '')
    if new_status not in BOOKING_TRANSITIONS or booking.status not in BOOKING_TRANSITIONS[new_status]:
        messages.error(request, 'Transición de cita no permitida.')
        return redirect('bookings')
    Booking.objects.filter(pk=booking.pk).update(status=new_status)
    _audit(request, 'booking.status', client=booking.client,
           target=f'booking:{booking.pk}', result=new_status)
    # AC M-05: the visitor hears about confirmations and cancellations.
    business = booking.client.name if booking.client else 'DOMINIO'
    local = timezone.localtime(booking.start)
    if new_status == 'confirmed':
        _send_plain_email(
            f'Tu cita quedó confirmada — {business}', [booking.email],
            f'¡Confirmado! Te esperamos el {local:%A %d de %B a las %I:%M %p}.\n\n— {business}')
    elif new_status == 'cancelled':
        _send_plain_email(
            f'Tu cita fue cancelada — {business}', [booking.email],
            f'Tu cita del {local:%A %d de %B a las %I:%M %p} fue cancelada. '
            f'Si quieres otra fecha, escríbenos o usa el chat.\n\n— {business}')
    messages.success(request, f'Cita marcada como {new_status}.')
    return redirect('bookings')


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
    AvailabilityRule.objects.update_or_create(
        client=client, weekday=weekday,
        defaults={'open_time': open_t, 'close_time': close_t})
    _audit(request, 'availability.set', client=client,
           target=f'weekday:{weekday}', result=f'{open_t}-{close_t}')
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
    return render(request, 'landing/dashboard_reports.html', context)


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
    qs = leads_for(request.user)
    source = request.GET.get('source', '')
    status = request.GET.get('status', '')
    q = (request.GET.get('q') or '').strip()
    if source in dict(ContactSubmission.SOURCE_CHOICES):
        qs = qs.filter(source=source)
    if status in dict(ContactSubmission.STATUS_CHOICES):
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q)
                       | Q(phone__icontains=q) | Q(company__icontains=q)
                       | Q(message__icontains=q))

    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = 'attachment; filename="leads.csv"'
    resp.write('﻿')  # BOM so Excel opens accents correctly
    writer = csv.writer(resp)
    writer.writerow(['Nombre', 'Email', 'Teléfono', 'Compañía', 'Servicio',
                     'Fuente', 'Estado', 'Página', 'Mensaje', 'Fecha'])
    for lead in qs[:5000]:
        writer.writerow([_csv_safe(v) for v in (
            lead.name, lead.email, lead.phone, lead.company,
            lead.get_service_display(), lead.get_source_display(),
            lead.get_status_display(), lead.page_url,
            lead.message[:1000], lead.created_at.strftime('%Y-%m-%d %H:%M'))])
    _audit(request, 'leads.export', target=f'{qs.count()} filas',
           result=f'filtros source={source} status={status} q={q[:40]}')
    return resp


# ============================================================
# M-15 / M-19 — Knowledge base + test-before-publish console
# ============================================================

def _knowledge_client(request):
    """Resolve which organization's knowledge is being managed. org_admin only."""
    qs = clients_for(request.user)
    slug = request.GET.get('client') or request.POST.get('client') or ''
    client = qs.filter(slug=slug).first() if slug else qs.first()
    if client is None or not is_org_admin(request.user, client):
        return None
    return client


def _knowledge_context(request, client, **extra):
    sources = list(client.knowledge_sources.exclude(status='archived').select_related('replaces'))
    archived = list(client.knowledge_sources.filter(status='archived'))
    # Phase 6: past versions per row, and which live source has a pending draft.
    history = publishing.history_for(sources, archived)
    pending_for = {s.replaces_id for s in sources if s.status in ('draft', 'error') and s.replaces_id}
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
        'scan_url': ingest.default_scan_url(client),
        'scan_running': any(s.kind == 'site' and s.in_progress for s in sources),
        'any_in_progress': any(s.in_progress for s in sources),
        'upload_max_mb': ingest.upload_max_bytes() // (1024 * 1024),
        'test_question': '', 'test_answer': None,
        'can_publish': publishing.can_publish(request.user, client),
        'review_message': publishing.REVIEW_MESSAGE,
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
        messages.error(request, 'Solo un administrador puede gestionar el conocimiento.')
        return redirect('dashboard')
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
        messages.error(request, 'Solo un administrador puede gestionar el conocimiento.')
        return redirect('dashboard')
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
    (the documented Django pattern) instead of by the middleware."""
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
        messages.error(request, 'Solo un administrador puede gestionar el conocimiento.')
        return redirect('dashboard')
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
        source = ingest.enqueue_file(client, uploaded)
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


def _after_draft_saved(request, client, source):
    """Starter / no plan: say who publishes, and tell DOMINIO (once a day)."""
    if not publishing.can_publish(request.user, client):
        messages.info(request, publishing.REVIEW_MESSAGE)
        publishing.request_review(source, request)


def _process_as_draft(source):
    """Fetch/extract into fragments without ever going live (Phase 6)."""
    return knowledge.process_source(source, success_status='draft')


@login_required
@require_POST
# Saving a URL source fetches it inside the request: cap how often.
@_rate_limited('ksave', limit=10, window=300, html='knowledge')
def knowledge_save(request):
    """Create or update a source; it lands as DRAFT (or error) — activation is a
    separate, deliberate step after testing (RF-21).

    Phase 6: editing an ACTIVE source never touches it. The edit goes into a
    draft copy (replaces=<active>, reused if one is already open) and the live
    version keeps answering until that draft is activated."""
    client = _knowledge_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar el conocimiento.')
        return redirect('dashboard')
    pk = request.POST.get('pk') or None
    source = (get_object_or_404(client.knowledge_sources, pk=pk) if pk
              else KnowledgeSource(client=client))
    if source.pk and source.in_progress:
        messages.error(request, 'Esa fuente se está procesando; espera a que termine.')
        return _knowledge_redirect(client)
    live = source if source.pk and source.status == 'active' else None
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
    source.status = 'draft'
    source.save()
    # Process now: extract/fetch → fragments; failures land visibly in 'error'.
    _process_as_draft(source)
    if source.status == 'error':
        messages.error(request, f'La fuente quedó en error: {source.error}')
    else:
        if live is not None:
            messages.success(
                request, f'Tus cambios quedaron en un borrador. La versión activa de '
                         f'"{live.title}" sigue publicada hasta que actives el borrador.')
        else:
            messages.success(
                request, 'Fuente procesada y en borrador. Pruébala y luego actívala.')
        _after_draft_saved(request, client, source)
    _audit(request, 'knowledge.save', client=client, target=source.title,
           result=source.status + (f' (nueva versión de #{live.pk})' if live else ''))
    return redirect(reverse('knowledge') + f'?client={client.slug}')


def _publish_denied(request, client, source, action):
    """Server-side plan gate: 403 with the page and the reason."""
    if action == 'activate' and source.status == 'draft':
        publishing.request_review(source, request)
    messages.error(request, 'Tu plan no incluye publicar cambios desde tu cuenta. '
                   + publishing.REVIEW_MESSAGE)
    _audit(request, f'knowledge.{action}', client=client, target=source.title,
           result='denegado: plan')
    return render(request, 'landing/dashboard_knowledge.html',
                  _knowledge_context(request, client), status=403)


@login_required
@require_POST
def knowledge_action(request, pk):
    client = _knowledge_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar el conocimiento.')
        return redirect('dashboard')
    source = get_object_or_404(client.knowledge_sources, pk=pk)
    action = request.POST.get('action', '')
    # Phase 6: anything that changes what the live agent says is publishing.
    publishes = action in ('activate', 'restore') or (
        action == 'archive' and source.status == 'active')
    if publishes and not publishing.can_publish(request.user, client):
        return _publish_denied(request, client, source, action)
    result = 'ok'
    if action in ('activate', 'restore'):
        override = request.user.is_staff and request.POST.get('override_caps') == '1'
        try:
            retired = publishing.publish(source, restoring=(action == 'restore'),
                                         override_caps=override)
        except publishing.PublishError as exc:
            messages.error(request, str(exc))
            result = f'rechazada: {exc}'
        else:
            verb = 'restaurada' if action == 'restore' else 'activa'
            note = ' La versión anterior quedó archivada.' if retired else ''
            messages.success(request, f'"{source.title}" está {verb} — el agente ya la usa.{note}')
            result = 'activa' + (' (límites ignorados por staff)' if override else '') + (
                f'; archivadas: {", ".join(retired)}' if retired else '')
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
    elif action == 'reprocess' and not source.in_progress:
        # Re-fetching a LIVE source writes into its draft copy, like an edit.
        target = source
        if source.status == 'active':
            target = publishing.open_draft_for(source)
            if target.pk is None:
                target.status = 'draft'
                target.save()
        _process_as_draft(target)
        messages.success(request, f'"{target.title}" reprocesada ({target.get_status_display()}).'
                         + (' La versión activa sigue publicada.' if target is not source else ''))
        if target.status == 'draft':
            _after_draft_saved(request, client, target)
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
        messages.error(request, 'Solo un administrador puede gestionar el conocimiento.')
        return redirect('dashboard')
    question = (request.POST.get('question') or '').strip()[:2000]
    draft_pk = request.POST.get('draft') or None
    test_answer = None
    if question and _tenant_action_cap_hit('ktest', client):
        messages.error(request, 'Llegaste al límite de pruebas de hoy para este agente. '
                                'Intenta de nuevo mañana.')
    elif question:
        prompt = client.compiled_prompt(include_draft_pk=draft_pk)
        try:
            test_answer, test_usage = agent.answer(
                [{'role': 'user', 'content': question}],
                business_prompt=prompt, handlers={},
                language=client.primary_language, **agent.clinic_kwargs(client))
        except agent.AgentNotConfigured:
            test_answer = '(El agente no está configurado — falta ANTHROPIC_API_KEY.)'
        except anthropic.APIError:
            logger.exception('Knowledge test call failed')
            test_answer = '(Error consultando el modelo — trata de nuevo.)'
        else:
            usage_ledger.record('knowledge_test', test_usage, client=client)
    return render(request, 'landing/dashboard_knowledge.html', _knowledge_context(
        request, client, test_question=question, test_answer=test_answer,
        tested_draft=str(draft_pk or '')))


# ============================================================
# M-17 — Users & roles per organization
# ============================================================

@login_required
def users_list(request):
    client = _knowledge_client(request)  # same org_admin gate
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar usuarios.')
        return redirect('dashboard')
    members = client.members.select_related('user')
    return render(request, 'landing/dashboard_users.html', {
        'active_tab': 'users',
        'client_obj': client, 'members': members,
        'all_clients': clients_for(request.user),
        'role_choices': Membership.ROLE_CHOICES,
    })


@login_required
@require_POST
def user_invite(request):
    from .views import _provision_client_login  # late import: avoids cycle at load
    client = _knowledge_client(request)
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

    # Reuse the provisioning path: a new account gets a one-time link to create
    # its password; an existing one (member of another agent) gets no link.
    original_notify = client.notify_email
    client.notify_email = email  # provision against the invited address
    username, setup_url = _provision_client_login(client)
    client.notify_email = original_notify
    if username:
        # Only the membership just created above (checked absent before).
        Membership.objects.filter(user__username=username, client=client).update(role=role)
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
    client = _knowledge_client(request)
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
        _audit(request, 'user.remove', client=client, target=membership.user.username)
        membership.delete()
    messages.success(request, 'Acceso eliminado (la cuenta sigue existiendo).')
    return redirect(reverse('dash_users') + f'?client={client.slug}')


# ============================================================
# M-17 — Audit trail
# ============================================================

@login_required
def audit_list(request):
    from .models import AuditEvent
    if request.user.is_staff:
        qs = AuditEvent.objects.select_related('client', 'user')
    elif is_org_admin(request.user):
        qs = AuditEvent.objects.filter(
            client__in=clients_for(request.user)).select_related('client', 'user')
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
