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

import anthropic
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Avg, Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import agent, knowledge
from .models import (
    AvailabilityRule, Booking, Client, ContactSubmission, Conversation,
    KnowledgeSource, Membership, Survey,
)
from .views import (
    _audit, _client_ip, _member_client_ids, _send_plain_email, leads_for,
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

@login_required
def reports(request):
    since = timezone.now() - timezone.timedelta(days=30)
    convs = conversations_for(request.user).filter(started_at__gte=since)
    leads = leads_for(request.user).filter(created_at__gte=since)

    n_convs = convs.count()
    n_chat_leads = leads.filter(source='chat').count()
    tokens = convs.aggregate(t=Sum('tokens_used'))['t'] or 0
    # Cost estimate: blended $/MTok knobs (defaults sized for Haiku-class models).
    in_rate = float(getattr(settings, 'DOMINIO_COST_PER_MTOK', 3.0))
    est_cost = tokens / 1_000_000 * in_rate
    csat = Survey.objects.filter(
        conversation__in=conversations_for(request.user),
        created_at__gte=since).aggregate(a=Avg('score'), n=Count('id'))

    stats = {
        'conversations': n_convs,
        'escalated': convs.filter(state='escalated').count(),
        'chat_leads': n_chat_leads,
        'conversion': round(n_chat_leads / n_convs * 100) if n_convs else 0,
        'leads_total': leads.count(),
        'leads_form': leads.filter(source='contact_form').count(),
        'leads_signup': leads.filter(source='signup').count(),
        'tokens': tokens,
        'est_cost': round(est_cost, 2),
        'csat_avg': round(csat['a'], 1) if csat['a'] else None,
        'csat_n': csat['n'],
    }

    # Escalation reasons = the raw material for the monthly tuning cycle (M-19).
    unanswered = list(
        convs.filter(state='escalated').exclude(escalation_reason='')
        .values_list('escalation_reason', flat=True)[:20])

    per_client = None
    if request.user.is_staff:
        per_client = (
            convs.values('client__name', 'client__slug')
            .annotate(n=Count('id'), tokens=Sum('tokens_used'),
                      leads=Count('lead', distinct=True))
            .order_by('-n')[:50])
        for row in per_client:
            row['cost'] = round((row['tokens'] or 0) / 1_000_000 * in_rate, 2)

    top_pages = (
        leads.exclude(page_url='').values('page_url')
        .annotate(n=Count('id')).order_by('-n')[:10])

    return render(request, 'landing/dashboard_reports.html', {
        'active_tab': 'reports',
        'stats': stats, 'unanswered': unanswered,
        'per_client': per_client, 'top_pages': top_pages,
    })


# ============================================================
# M-07 — CSV export (audited, scoped, Excel-friendly)
# ============================================================

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
        writer.writerow([
            lead.name, lead.email, lead.phone, lead.company,
            lead.get_service_display(), lead.get_source_display(),
            lead.get_status_display(), lead.page_url,
            lead.message[:1000], lead.created_at.strftime('%Y-%m-%d %H:%M')])
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


@login_required
def knowledge_list(request):
    client = _knowledge_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar el conocimiento.')
        return redirect('dashboard')
    sources = client.knowledge_sources.exclude(status='archived')
    archived = client.knowledge_sources.filter(status='archived')
    return render(request, 'landing/dashboard_knowledge.html', {
        'active_tab': 'knowledge',
        'client_obj': client, 'sources': sources, 'archived': archived,
        'all_clients': clients_for(request.user),
        'kind_choices': KnowledgeSource.KIND_CHOICES,
        'test_question': '', 'test_answer': None,
    })


@login_required
@require_POST
def knowledge_save(request):
    """Create or update a source; it lands as DRAFT (or error) — activation is a
    separate, deliberate step after testing (RF-21)."""
    client = _knowledge_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar el conocimiento.')
        return redirect('dashboard')
    pk = request.POST.get('pk') or None
    source = (get_object_or_404(client.knowledge_sources, pk=pk) if pk
              else KnowledgeSource(client=client))
    kind = request.POST.get('kind', 'text')
    source.kind = kind if kind in dict(KnowledgeSource.KIND_CHOICES) else 'text'
    source.title = (request.POST.get('title') or '').strip()[:160] or 'Sin título'
    source.origin = (request.POST.get('origin') or '').strip()[:500]
    source.content = (request.POST.get('content') or '').strip()[:60000]
    source.status = 'draft'
    source.save()
    # Process now: extract/fetch → fragments; failures land visibly in 'error'.
    knowledge.process_source(source)
    if source.status == 'error':
        messages.error(request, f'La fuente quedó en error: {source.error}')
    else:
        # Processing succeeded but activation is explicit — back to draft.
        KnowledgeSource.objects.filter(pk=source.pk).update(status='draft')
        messages.success(
            request, 'Fuente procesada y en borrador. Pruébala y luego actívala.')
    _audit(request, 'knowledge.save', client=client, target=source.title,
           result=source.status)
    return redirect(reverse('knowledge') + f'?client={client.slug}')


@login_required
@require_POST
def knowledge_action(request, pk):
    client = _knowledge_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar el conocimiento.')
        return redirect('dashboard')
    source = get_object_or_404(client.knowledge_sources, pk=pk)
    action = request.POST.get('action', '')
    if action == 'activate' and source.status == 'draft' and source.compiled_text():
        KnowledgeSource.objects.filter(pk=source.pk).update(status='active', error='')
        messages.success(request, f'"{source.title}" está activa — el agente ya la usa.')
    elif action == 'archive':
        KnowledgeSource.objects.filter(pk=source.pk).update(status='archived')
        messages.success(request, f'"{source.title}" archivada.')
    elif action == 'reprocess':
        knowledge.process_source(source)
        if source.status != 'error':
            KnowledgeSource.objects.filter(pk=source.pk).update(status='draft')
        messages.success(request, f'"{source.title}" reprocesada ({source.status}).')
    elif action == 'delete' and source.status in ('draft', 'error', 'archived'):
        source.delete()
        messages.success(request, 'Fuente eliminada.')
    else:
        messages.error(request, 'Acción no permitida para el estado de esta fuente.')
    _audit(request, f'knowledge.{action}', client=client, target=source.title)
    return redirect(reverse('knowledge') + f'?client={client.slug}')


@login_required
@require_POST
def knowledge_test(request):
    """RF-21/M-19: ask a test question against the compiled knowledge (optionally
    including ONE draft source) before anything goes live. Never persists."""
    client = _knowledge_client(request)
    if client is None:
        messages.error(request, 'Solo un administrador puede gestionar el conocimiento.')
        return redirect('dashboard')
    question = (request.POST.get('question') or '').strip()[:2000]
    draft_pk = request.POST.get('draft') or None
    test_answer = None
    if question:
        prompt = client.compiled_prompt(include_draft_pk=draft_pk)
        try:
            test_answer, _u = agent.answer(
                [{'role': 'user', 'content': question}],
                business_prompt=prompt, handlers={},
                language=client.primary_language)
        except agent.AgentNotConfigured:
            test_answer = '(El agente no está configurado — falta ANTHROPIC_API_KEY.)'
        except anthropic.APIError:
            logger.exception('Knowledge test call failed')
            test_answer = '(Error consultando el modelo — trata de nuevo.)'
    sources = client.knowledge_sources.exclude(status='archived')
    return render(request, 'landing/dashboard_knowledge.html', {
        'active_tab': 'knowledge',
        'client_obj': client, 'sources': sources,
        'archived': client.knowledge_sources.filter(status='archived'),
        'all_clients': clients_for(request.user),
        'kind_choices': KnowledgeSource.KIND_CHOICES,
        'test_question': question, 'test_answer': test_answer,
        'tested_draft': str(draft_pk or ''),
    })


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

    # Reuse the provisioning path (new user gets a temp password by email).
    original_notify = client.notify_email
    client.notify_email = email  # provision against the invited address
    username, temp_password = _provision_client_login(client)
    client.notify_email = original_notify
    if username:
        Membership.objects.filter(user__username=username, client=client).update(role=role)
        body = (f'Te invitaron al dashboard de {client.name} en DOMINIO Chat 24/7.\n\n'
                f'Entra en: {request.build_absolute_uri(reverse("dashboard"))}\n'
                f'Usuario: {username}\n')
        if temp_password:
            body += (f'Contraseña temporal: {temp_password}\n'
                     'Al entrar, el sistema te pedirá crear tu propia contraseña.')
        else:
            body += 'Usa tu contraseña existente (tu cuenta ya administraba otro agente).'
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
