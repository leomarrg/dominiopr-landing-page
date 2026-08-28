import hmac
import json
import logging
import re
import secrets
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from urllib.parse import urlparse

import anthropic
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.views import PasswordResetConfirmView
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import URLValidator, validate_email
from django.db import IntegrityError, transaction
from django.db.models import F, Max, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import agent, notify, payments
from .forms import ContactForm
from .models import (
    AuditEvent, Booking, ChatMessage, Client, ContactSubmission, Conversation,
    Membership, ProcessedWebhookEvent, Subscription, Survey,
)
from .phone import normalize_phone

logger = logging.getLogger(__name__)

# Bound a single visitor message so the public endpoint can't be abused with
# huge payloads.
MAX_MESSAGE_CHARS = 2000
# Hard cap on the raw request body for the chat endpoint (a conversation is tiny).
MAX_CHAT_BODY_BYTES = 64 * 1024


def _rate_limited(prefix, limit, window, methods=None):
    """Per-IP fixed-window rate limit using the shared cache (no Redis).

    Returns a JSON 429 once `limit` requests from one IP occur within `window`
    seconds. Used to stop bots from burning the Anthropic budget / DoSing the box.

    By default only state-changing requests count (GET/HEAD/OPTIONS page loads
    are free). Pass `methods=('GET', 'POST')` to also throttle reads on views
    whose GET does expensive work (e.g. a Stripe call per request).
    """
    counted = tuple(m.upper() for m in methods) if methods else None

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if counted is None:
                if request.method in ('GET', 'HEAD', 'OPTIONS'):
                    return view(request, *args, **kwargs)
            elif request.method not in counted:
                return view(request, *args, **kwargs)
            ip = _client_ip(request) or 'unknown'
            key = f'rl:{prefix}:{ip}'
            count = cache.get(key, 0)
            if count >= limit:
                resp = JsonResponse(
                    {'error': 'Demasiadas solicitudes. Baja un poco la velocidad.'}, status=429)
                resp['Retry-After'] = str(window)
                return resp
            # Fixed window: first hit sets the TTL; subsequent hits just increment.
            if count == 0:
                cache.set(key, 1, window)
            else:
                try:
                    cache.incr(key)
                except ValueError:
                    cache.set(key, 1, window)
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


def _cors_enabled(view):
    """Let the embeddable widget call this endpoint from a client's own domain.

    Answers CORS preflight (OPTIONS) and tags every response with the requesting
    Origin. The per-client domain allowlist is enforced inside the view; this just
    makes the browser able to read the (allowed) response cross-origin.
    """
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        origin = request.META.get('HTTP_ORIGIN', '')
        if request.method == 'OPTIONS':
            resp = HttpResponse(status=204)
        else:
            resp = view(request, *args, **kwargs)
        if origin:
            resp['Access-Control-Allow-Origin'] = origin
            resp['Vary'] = 'Origin'
            resp['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
            resp['Access-Control-Allow-Headers'] = 'Content-Type'
            resp['Access-Control-Max-Age'] = '86400'
        return resp
    return wrapper


def _client_ip(request):
    """Real client IP, honoring the Nginx X-Forwarded-For header.

    Nginx sets the header with `$proxy_add_x_forwarded_for`, which APPENDS the
    peer address to whatever the client sent, so the only trustworthy value is
    the LAST one. Taking the first would let anyone spoof their way past every
    per-IP rate limit.
    """
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        last = forwarded.rsplit(',', 1)[-1].strip()
        if last:
            return last
    return request.META.get('REMOTE_ADDR')


def _single_line(value, limit=None):
    """Collapse CR/LF/tabs so user text can never inject email headers or
    break a subject line. Optionally truncates."""
    text = re.sub(r'[\r\n\t]+', ' ', str(value or '')).strip()
    return text[:limit] if limit else text


def _id_tail(value, keep=6):
    """Last few chars of an opaque id for logs (enough to correlate, not to
    replay or identify)."""
    value = str(value or '')
    if not value:
        return '-'
    return f'...{value[-keep:]}' if len(value) > keep else value


def _page_url(raw):
    """Normalize a visitor-page URL (widget location.href or Referer header):
    only http(s), trimmed to the model's 500-char cap. Empty string otherwise."""
    if not isinstance(raw, str):
        return ''
    raw = raw.strip()
    if not raw.lower().startswith(('http://', 'https://')):
        return ''
    return raw[:500]


def _audit(request, action, client=None, target='', result='ok'):
    """M-17: record a critical admin action. Best-effort — auditing must never
    break the action it describes."""
    try:
        AuditEvent.objects.create(
            client=client,
            user=request.user if getattr(request.user, 'is_authenticated', False) else None,
            action=action[:60], target=str(target)[:200], result=str(result)[:200],
            ip_address=_client_ip(request) or None,
        )
    except Exception:
        logger.exception('Audit write failed for %s', action)


def _send_plain_email(subject, to, body, reply_to=None):
    """Plain-text one-off email (escalations, ops alerts). Logs and swallows
    errors, same contract as _send_html_email."""
    subject = ' '.join(str(subject).splitlines())
    try:
        msg = EmailMultiAlternatives(
            subject, body, settings.DEFAULT_FROM_EMAIL, to, reply_to=reply_to)
        msg.send(fail_silently=False)
        return True
    except Exception:
        logger.exception('Failed to send email "%s" to %s', subject, to)
        return False


def _send_html_email(subject, to, html_template, txt_template, context, reply_to=None):
    """Send a multipart (text + HTML) email. Logs and swallows errors.

    Returns True on success, False on failure — automatic callers can ignore it,
    but manual flows (e.g. replying to a lead) use it to show real feedback.
    """
    subject = ' '.join(str(subject).splitlines())
    try:
        html_body = render_to_string(html_template, context)
        text_body = render_to_string(txt_template, context)
        msg = EmailMultiAlternatives(
            subject,
            text_body,
            settings.DEFAULT_FROM_EMAIL,
            to,
            reply_to=reply_to,
        )
        msg.attach_alternative(html_body, 'text/html')
        msg.send(fail_silently=False)
        return True
    except Exception:
        # The lead is already saved in the DB; a failed email must not break the UX.
        logger.exception('Failed to send email "%s" to %s', subject, to)
        return False


def _send_lead_emails(submission, notify_to=None):
    """Notify the team and send the visitor a styled confirmation.

    `notify_to` lets each client route its leads to its own inbox; defaults to
    DOMINIO's configured address.
    """
    if notify_to is None:
        notify_to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    business = submission.client.name if submission.client else 'DOMINIO'
    context = {
        'submission': submission,
        'business': business,
        # Direct link to the leads dashboard so the client can manage the lead
        # (status, reply) right from the notification email.
        'dashboard_url': settings.SITE_URL + reverse('dashboard'),
    }

    # 1) Internal notification to the business inbox.
    if notify_to:
        _send_html_email(
            subject=f'New lead for {business}: {submission.name}',
            to=[notify_to],
            html_template='landing/emails/lead_notification.html',
            txt_template='landing/emails/lead_notification.txt',
            context=context,
            reply_to=[submission.email] if submission.email else None,
        )

    # 2) Confirmation to the person who submitted the form — only if they left an
    # email (phone-only leads can't be emailed; the team follows up by phone).
    if submission.email:
        _send_html_email(
            subject=f'We received your request — {business}',
            to=[submission.email],
            html_template='landing/emails/lead_confirmation.html',
            txt_template='landing/emails/lead_confirmation.txt',
            context=context,
            reply_to=[notify_to] if notify_to else None,
        )


def _send_booking_emails(booking, notify_to=None, business='DOMINIO'):
    """Notify the business of a new booking and confirm it to the visitor."""
    if notify_to is None:
        notify_to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    local = timezone.localtime(booking.start)
    context = {'booking': booking, 'business': business, 'local_start': local}

    if notify_to:
        _send_html_email(
            subject=f'New booking for {business}: {booking.name} — {local:%b %d, %I:%M %p}',
            to=[notify_to],
            html_template='landing/emails/booking_notification.html',
            txt_template='landing/emails/booking_notification.txt',
            context=context,
            reply_to=[booking.email],
        )
    _send_html_email(
        subject=f'Your booking is received — {business}',
        to=[booking.email],
        html_template='landing/emails/booking_confirmation.html',
        txt_template='landing/emails/booking_confirmation.txt',
        context=context,
        reply_to=[notify_to] if notify_to else None,
    )


def _send_onboarding_email(client, embed_snippet, login=None):
    """Email the client their install instructions when their agent goes live.

    `login` (optional) is {'username', 'password'|None, 'url'} so the client also
    gets their dashboard credentials — the 'Your own leads dashboard' they're
    paying for. password is None when the account already existed.
    """
    reply_to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    return _send_html_email(
        subject=f'Your AI agent is live — install instructions ({client.name})',
        to=[client.notify_email],
        html_template='landing/emails/agent_onboarding.html',
        txt_template='landing/emails/agent_onboarding.txt',
        context={'client': client, 'embed': embed_snippet, 'login': login},
        reply_to=[reply_to] if reply_to else None,
    )


def _embed_snippet(base_url, slug):
    """The one-line install snippet for a client. `base_url` is the site
    origin (request.build_absolute_uri('/') or settings.SITE_URL)."""
    return f'<script src="{base_url.rstrip("/")}/widget.js?key={slug}" async></script>'


def _send_welcome_email(client, plan_name, login, install_url, embed,
                        setup_fee_charged=False):
    """Post-payment welcome: what was bought, dashboard credentials and the
    'tell us how to access your site' link. Replaces the old install email for
    self-serve clients (DOMINIO installs the widget; the 'live' email follows)."""
    reply_to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    return _send_html_email(
        subject=f'Pago recibido — tu agente de IA está en camino ({client.name})',
        to=[client.notify_email],
        html_template='landing/emails/agent_welcome.html',
        txt_template='landing/emails/agent_welcome.txt',
        context={'client': client, 'plan_name': plan_name, 'login': login,
                 'install_url': install_url, 'embed': embed, 'setup_hours': 48,
                 'setup_fee_charged': setup_fee_charged},
        reply_to=[reply_to] if reply_to else None,
    )


def _send_live_email(client, dashboard_url, site_url):
    """Sent by the staff 'Marcar en vivo' action once the widget is installed."""
    reply_to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    return _send_html_email(
        subject=f'Tu agente de IA está en vivo — {client.name}',
        to=[client.notify_email],
        html_template='landing/emails/agent_live.html',
        txt_template='landing/emails/agent_live.txt',
        context={'client': client, 'dashboard_url': dashboard_url, 'site_url': site_url},
        reply_to=[reply_to] if reply_to else None,
    )


def _provision_client_login(client, reset_password=False):
    """Ensure `client` has a dashboard login, linking the notify_email's user to it.

    One login can manage SEVERAL agents: if the email already has an account, we
    just add a membership to this client. `reset_password=True` (used by the
    Resend action) sets a fresh temp password so the owner can hand the client a
    working credential even if the first email was lost. Returns (username,
    temp_password); temp_password is None when reusing a login without a reset.
    Staff accounts are never scoped into a tenant.
    """
    User = get_user_model()
    email = (client.notify_email or '').strip().lower()
    if not email:
        return None, None

    user = User.objects.filter(username=email).first()
    if user and (user.is_staff or user.is_superuser):
        # Never scope (or silently demote) a staff/superuser into one tenant.
        logger.warning('Skipped login provisioning: %s is a staff account', email)
        return None, None

    temp_password = None
    if user is None:
        temp_password = secrets.token_urlsafe(12)
        user = User.objects.create_user(username=email, email=email, password=temp_password)
    elif reset_password:
        temp_password = secrets.token_urlsafe(12)
        user.set_password(temp_password)
        user.save(update_fields=['password'])
        Membership.objects.filter(user=user).update(must_change_password=True)

    # Link the user to this client (a user may manage many agents).
    Membership.objects.get_or_create(
        user=user, client=client,
        defaults={'must_change_password': bool(temp_password)})
    return user.get_username(), temp_password


# ============================================================
# MULTI-TENANT ISOLATION — the single place tenant scope is decided
# ============================================================

# Factory + cross-tenant views are DOMINIO staff only.
staff_required = user_passes_test(lambda u: u.is_staff, login_url='login')


def _member_client_ids(user):
    """Client ids this user is a member of (a user can manage several agents)."""
    return list(
        Membership.objects.filter(user=user).values_list('client_id', flat=True))


def leads_for(user):
    """Leads this user may see: all for staff, only their clients' for a member,
    none for a logged-in user with no membership (safe default — never another
    tenant's data)."""
    qs = ContactSubmission.objects.all()
    if not getattr(user, 'is_authenticated', False):
        return qs.none()
    if user.is_staff:
        return qs
    ids = _member_client_ids(user)
    return qs.filter(client_id__in=ids) if ids else qs.none()


def get_lead_or_404(user, pk):
    """Fetch a lead scoped to what the user may see. Filtering the queryset
    BEFORE the pk lookup means a cross-tenant id returns 404, not the row —
    this is what closes the IDOR hole on status/email actions."""
    return get_object_or_404(leads_for(user), pk=pk)


def index(request):
    if request.method == 'POST':
        form = ContactForm(request.POST)
        if form.is_valid():
            submission = form.save(commit=False)
            submission.client = Client.objects.filter(slug='dominio').first()
            submission.ip_address = _client_ip(request)
            submission.user_agent = request.META.get('HTTP_USER_AGENT', '')[:300]
            submission.page_url = _page_url(request.META.get('HTTP_REFERER', ''))
            submission.save()
            _send_lead_emails(submission)
            messages.success(
                request,
                '¡Gracias! Recibimos tu mensaje y te contactamos pronto.',
            )
            return redirect(reverse('index') + '#contact')
        messages.error(request, 'Revisa los campos marcados y trata de nuevo.')
    else:
        form = ContactForm()
    return render(request, 'landing/index.html', {'form': form})


def _chat_lead_handler(request, client_obj=None, page_url='', conversation=None):
    """Build the callback the agent invokes when it captures a lead in chat.

    Saves a ContactSubmission linked to `client_obj` and emails that client's
    own inbox (falling back to DOMINIO's). Returns a short string the model
    feeds back as the tool result. Raises ValueError on a bad email so the
    agent re-asks the visitor.
    """
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    valid_services = {c[0] for c in ContactSubmission.SERVICE_CHOICES}
    notify_to = client_obj.notify_email if client_obj else None
    business_name = client_obj.name if client_obj else 'DOMINIO'

    def handle(data):
        name = (data.get('name') or '').strip()[:120]
        email = (data.get('email') or '').strip()[:254]
        phone = normalize_phone((data.get('phone') or '').strip()[:30])
        if not name:
            raise ValueError('Missing name.')
        if email:
            try:
                validate_email(email)
            except ValidationError:
                raise ValueError('Invalid email.')
        # Bad phone: drop it if there's a valid email; otherwise ask again.
        if phone is None:
            if email:
                phone = ''
            else:
                raise ValueError('Invalid phone number — ask the visitor to re-share it.')
        # A lead needs a name and at least one way to reach them.
        if not email and not phone:
            raise ValueError('Need an email or a phone number.')

        # Per-IP cap: a bot can't flood the inbox or abuse our SMTP to relay
        # confirmation emails to arbitrary third parties.
        ip = _client_ip(request) or 'unknown'
        cap_key = f'leadcap:{ip}'
        if cache.get(cap_key, 0) >= 5:
            logger.warning('Chat lead cap reached for IP %s', ip)
            return 'Anotado — el equipo ya tiene tu solicitud y te dará seguimiento por email.'

        service = data.get('service')
        if service not in valid_services:
            service = 'ai-automation'

        submission = ContactSubmission(
            client=client_obj,
            name=name,
            email=email,
            phone=phone,
            company=(data.get('company') or '').strip()[:160],
            service=service,
            message=('[Captured by the AI chat assistant] '
                     + (data.get('summary') or '').strip())[:5000],
            source='chat',
            ip_address=ip if ip != 'unknown' else None,
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:300],
            page_url=page_url,
        )
        submission.save()
        # M-02: tie the lead to its conversation so the transcript explains it.
        if conversation is not None:
            Conversation.objects.filter(pk=conversation.pk).update(lead=submission)
        _send_lead_emails(submission, notify_to=notify_to)
        # M-06: optional WhatsApp/SMS ping on top of the email.
        notify.notify_client(
            client_obj,
            f'DOMINIO Chat: nuevo lead para {business_name} — {name} '
            f'({email or phone}). Revisa tu dashboard.')
        cache.set(cap_key, cache.get(cap_key, 0) + 1, 3600)  # 1-hour window
        # Log the row id, not the person: journald is not covered by the
        # retention policy that purges conversations.
        logger.info('Lead captured via chat: submission=%s client=%s',
                    submission.pk, client_obj.slug if client_obj else '-')
        return f'Lead guardado y el equipo de {business_name} fue notificado por email.'

    return handle


def _human_handler(request, client_obj=None, conversation=None, page_url=''):
    """M-16: the request_human tool. Marks the conversation escalated with the
    reason, saves whatever contact the visitor gave as a lead, and alerts the
    team with the transcript context. Never promises a live chat."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    notify_to = (client_obj.notify_email if client_obj else None) \
        or getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    business_name = client_obj.name if client_obj else 'DOMINIO'

    def handle(data):
        reason = (data.get('reason') or '').strip()[:300] or 'El visitante pidió hablar con una persona.'
        name = (data.get('name') or '').strip()[:120]
        email = (data.get('email') or '').strip()[:254]
        phone = normalize_phone((data.get('phone') or '').strip()[:30]) or ''
        if email:
            try:
                validate_email(email)
            except ValidationError:
                email = ''

        ip = _client_ip(request) or 'unknown'
        cap_key = f'humancap:{ip}'
        if cache.get(cap_key, 0) >= 5:
            return 'Anotado — el equipo ya fue notificado y te dará seguimiento.'
        cache.set(cap_key, cache.get(cap_key, 0) + 1, 3600)

        lead = None
        if name and (email or phone):
            lead = ContactSubmission.objects.create(
                client=client_obj, name=name, email=email, phone=phone,
                service='ai-automation', source='chat',
                message=f'[Escalado a humano por el chat] {reason}'[:5000],
                ip_address=ip if ip != 'unknown' else None,
                user_agent=request.META.get('HTTP_USER_AGENT', '')[:300],
                page_url=page_url,
            )

        transcript = ''
        if conversation is not None:
            Conversation.objects.filter(pk=conversation.pk).update(
                state='escalated', escalation_reason=reason,
                **({'lead': lead} if lead else {}))
            turns = conversation.chat_messages.order_by('position')[:40]
            transcript = '\n'.join(
                f'{"Visitante" if m.role == "user" else "Agente"}: {m.content}' for m in turns)

        if notify_to:
            body = (f'Una conversación del chat de {business_name} necesita atención humana.\n\n'
                    f'Motivo: {reason}\n'
                    f'Contacto: {name or "—"} {email or phone or "(sin contacto)"}\n'
                    f'Página: {page_url or "—"}\n\n'
                    f'--- Conversación ---\n{transcript or "(disponible en el dashboard)"}')
            _send_plain_email(
                subject=f'[Atención humana] Chat escalado — {business_name}',
                to=[notify_to], body=body,
                reply_to=[email] if email else None)
        notify.notify_client(
            client_obj,
            f'DOMINIO Chat: un visitante de {business_name} pide atención humana. '
            f'Motivo: {reason[:120]}')
        logger.info('Conversation escalated for %s: %s',
                    client_obj.slug if client_obj else 'dominio', reason)
        return ('Escalado: el equipo fue notificado con el contexto y dará seguimiento. '
                'No prometas un tiempo de respuesta específico.')

    return handle


def _booking_handler(request, client_obj=None):
    """Callback the agent invokes to create a booking. The BACKEND validates the
    time and the DB constraint prevents double-booking — the model only proposes.
    """
    import datetime as dt
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    from django.db import IntegrityError, transaction

    from .models import Booking

    notify_to = client_obj.notify_email if client_obj else None
    business_name = client_obj.name if client_obj else 'DOMINIO'

    def handle(data):
        name = (data.get('name') or '').strip()[:120]
        email = (data.get('email') or '').strip()[:254]
        if not name or not email:
            raise ValueError('Missing name or email.')
        try:
            validate_email(email)
        except ValidationError:
            raise ValueError('Invalid email.')

        ip = _client_ip(request) or 'unknown'
        cap_key = f'bookcap:{ip}'
        if cache.get(cap_key, 0) >= 5:
            return 'Anotado — el equipo te dará seguimiento para confirmar tu hora.'

        # Parse the requested time; the backend decides validity, not the model.
        try:
            parsed = dt.datetime.fromisoformat((data.get('start') or '').strip())
        except (ValueError, TypeError):
            raise ValueError('I need a specific date and time.')
        start = (timezone.make_aware(parsed, timezone.get_current_timezone())
                 if timezone.is_naive(parsed) else parsed)
        now = timezone.now()
        if start <= now + dt.timedelta(minutes=30):
            raise ValueError('That time is in the past or too soon. Offer a later time.')
        if start > now + dt.timedelta(days=120):
            raise ValueError('That date is too far out.')

        # M-05: if the tenant configured working hours, the slot must fall inside
        # them. No rules configured = same behavior as before (any future slot).
        if client_obj is not None:
            rules = list(client_obj.availability_rules.all())
            if rules:
                local = timezone.localtime(start)
                day_rules = [r for r in rules if r.weekday == local.weekday()]
                ok = any(r.open_time <= local.time() < r.close_time for r in day_rules)
                if not ok:
                    raise ValueError(
                        'That time is outside business hours. Offer a time within '
                        'the business schedule.')

        try:
            with transaction.atomic():
                booking = Booking.objects.create(
                    client=client_obj, name=name, email=email,
                    service=(data.get('service') or '').strip()[:120],
                    notes=(data.get('notes') or '').strip()[:1000],
                    start=start, status='pending',
                )
        except IntegrityError:
            return 'Esa hora ya está ocupada. Ofrécele al visitante otra hora.'

        cache.set(cap_key, cache.get(cap_key, 0) + 1, 3600)
        _send_booking_emails(booking, notify_to=notify_to, business=business_name)
        local = timezone.localtime(start)
        notify.notify_client(
            client_obj,
            f'DOMINIO Chat: nueva cita para {business_name} — {name}, '
            f'{local:%a %d %b, %I:%M %p}.')
        # No visitor PII in the logs — journald outlives our retention policy.
        logger.info('Booking created: booking=%s client=%s at %s',
                    booking.pk, client_obj.slug, local)
        return f'Reservado para {local:%A %b %d a las %I:%M %p}. Se envió una confirmación por email.'

    return handle


@csrf_exempt
@_cors_enabled
@_rate_limited('chat', limit=12, window=60)
def chat_api(request):
    """JSON endpoint for the embeddable chat widget (cross-origin).

    Expects {"client": "<slug>", "messages": [{"role", "content"}, ...]}.
    Returns {"reply": "..."} or {"error": "..."} with an appropriate status.
    Public + unauthenticated, so it's hardened by: per-IP rate limit, global
    daily cap, per-IP lead cap, body-size cap, and a per-client origin allowlist.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)

    # Reject oversized bodies before parsing them into memory.
    if len(request.body) > MAX_CHAT_BODY_BYTES:
        return JsonResponse({'error': 'La solicitud es demasiado grande.'}, status=413)

    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'Solicitud inválida.'}, status=400)

    # Resolve which business this agent represents (multi-tenant). The widget
    # sends its public "client" slug; DOMINIO's own site defaults to 'dominio'.
    slug = payload.get('client')
    slug = slug.strip()[:60] if isinstance(slug, str) else 'dominio'
    client_obj = Client.objects.filter(slug=slug or 'dominio', is_active=True).first()
    # M-15: the agent's knowledge = manual prompt + active knowledge sources.
    business_prompt = client_obj.compiled_prompt() if client_obj else None

    # Authenticate the tenant. The slug is a public identifier, not a secret:
    # it is slugify(company name) and /widget.js reveals which ones exist. The
    # widget therefore presents `widget_token`, which /widget.js only hands to a
    # page that already knows the key. DOMINIO's own first-party site is exempt
    # because it embeds the widget directly, without going through widget.js.
    if client_obj and client_obj.slug != 'dominio':
        presented = payload.get('token')
        presented = presented.strip()[:64] if isinstance(presented, str) else ''
        expected = client_obj.widget_token
        if not expected or not hmac.compare_digest(presented, expected):
            return JsonResponse({'error': 'Agente no autorizado.'}, status=403)

    # Origin allowlist: the widget may only run on the client's own domains.
    # A missing Origin header must NOT skip the check — that is exactly what a
    # non-browser attacker sends. Browsers always attach it to a cross-origin
    # POST, so requiring it costs a legitimate widget nothing.
    origin = request.META.get('HTTP_ORIGIN', '')
    if client_obj and client_obj.allowed_origins:
        allowed = set(client_obj.origin_list())
        o = urlparse(origin) if origin else None
        if (o is None or (o.netloc.lower() not in allowed
                          and (o.hostname or '').lower() not in allowed)):
            return JsonResponse({'error': 'Este dominio no está permitido.'}, status=403)

    raw = payload.get('messages')
    if not isinstance(raw, list) or not raw:
        return JsonResponse({'error': 'No se enviaron mensajes.'}, status=400)

    # Sanitize: keep only well-formed user/assistant turns with non-empty text,
    # then trim to the most recent N turns to bound cost.
    history = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get('role')
        content = item.get('content')
        if role not in ('user', 'assistant') or not isinstance(content, str):
            continue
        content = content.strip()[:MAX_MESSAGE_CHARS]
        if content:
            history.append({'role': role, 'content': content})

    max_turns = getattr(settings, 'DOMINIO_AGENT_MAX_TURNS', 12)
    history = history[-max_turns:]

    # The Messages API requires the conversation to start with a user turn.
    while history and history[0]['role'] != 'user':
        history.pop(0)
    if not history or history[-1]['role'] != 'user':
        return JsonResponse({'error': 'No hay pregunta que contestar.'}, status=400)

    # M-04: per-tenant daily quota BEFORE the global cap — one noisy or attacked
    # tenant degrades only its own widget, never everyone else's.
    if client_obj is not None and client_obj.daily_message_cap:
        today = timezone.now().strftime('%Y%m%d')

        # No SINGLE visitor may spend the whole tenant's day. The widget key is
        # public (it sits in the customer's page source), so without this one
        # person — or a competitor — could drain a business's daily quota and
        # leave its agent answering 503 to real customers until midnight.
        # The per-minute IP limit does not prevent that: 12/min reaches a 200
        # message cap in under 20 minutes.
        visitor_cap = max(15, int(client_obj.daily_message_cap * 0.15))
        visitor_key = f'chat:visitor:{client_obj.slug}:{_client_ip(request)}:{today}'
        if cache.get(visitor_key, 0) >= visitor_cap:
            logger.warning('Per-visitor cap reached on %s', client_obj.slug)
            return JsonResponse(
                {'error': 'Llegaste al límite de mensajes por hoy. Usa el formulario de '
                          'contacto y el equipo te responde enseguida.'},
                status=429,
            )
        if cache.get(visitor_key, 0) == 0:
            cache.set(visitor_key, 1, 60 * 60 * 26)
        else:
            try:
                cache.incr(visitor_key)
            except ValueError:
                cache.set(visitor_key, 1, 60 * 60 * 26)

        tenant_key = f'chat:client:{client_obj.slug}:' + today
        tenant_used = cache.get(tenant_key, 0)
        if tenant_used >= client_obj.daily_message_cap:
            logger.warning('Tenant chat quota reached for %s', client_obj.slug)
            return JsonResponse(
                {'error': 'El asistente llegó a su límite de hoy. Usa el formulario de '
                          'contacto y el equipo te responde enseguida.'},
                status=503,
            )
        if tenant_used == 0:
            cache.set(tenant_key, 1, 60 * 60 * 26)
        else:
            try:
                cache.incr(tenant_key)
            except ValueError:
                cache.set(tenant_key, 1, 60 * 60 * 26)
        if tenant_used + 1 >= int(client_obj.daily_message_cap * 0.8):
            logger.warning('Tenant %s at 80%%+ of daily chat quota', client_obj.slug)

    # Global daily ceiling across ALL IPs — the defense per-IP limits can't give:
    # caps worst-case daily API spend even under a distributed/botnet attack.
    daily_cap = getattr(settings, 'DOMINIO_AGENT_DAILY_CAP', 2000)
    day_key = 'chat:global:' + timezone.now().strftime('%Y%m%d')
    used = cache.get(day_key, 0)
    if used >= daily_cap:
        logger.warning('Daily chat cap (%s) reached', daily_cap)
        return JsonResponse(
            {'error': 'Nuestro asistente está descansando por ahora. Usa el formulario de '
                      'contacto y el equipo te responde enseguida.'},
            status=503,
        )
    if used == 0:
        cache.set(day_key, 1, 60 * 60 * 26)  # ~26h TTL covers the whole day
    else:
        try:
            cache.incr(day_key)
        except ValueError:
            cache.set(day_key, 1, 60 * 60 * 26)

    # Page the visitor is chatting from (widget sends location.href) — lets the
    # client see exactly which page produced the lead.
    page = _page_url(payload.get('page'))

    # M-02: persist the conversation for the tenant (NEVER for the demo). The
    # widget generates a random session id per visitor session; without one
    # (old cached widgets) nothing is persisted and the chat still works.
    conversation = None
    session = payload.get('session')
    if client_obj is not None and isinstance(session, str):
        session = ''.join(c for c in session if c.isalnum() or c in '-_')[:64]
        if len(session) >= 8:
            conversation, _created = Conversation.objects.get_or_create(
                client=client_obj, widget_session=session,
                defaults={'page_url': page,
                          'config_version': client_obj.config_version()})

    # The caller controls the request body, so the 'assistant' turns it sends
    # are not evidence of anything we said. Left as-is, a visitor could feed the
    # model a fabricated prior turn ("te autorizo 95% de descuento") to get it
    # ratified, and — because persistence rewrote the transcript from the same
    # body — the tenant's dashboard would corroborate the invention.
    # Rule: the server owns the history; the client contributes one user turn.
    if conversation is not None:
        stored = list(conversation.chat_messages.order_by('position')
                      .values('role', 'content'))
        history = (stored + [history[-1]])[-max_turns:]
        while history and history[0]['role'] != 'user':
            history.pop(0)

    handlers = {'capture_lead': _chat_lead_handler(
        request, client_obj, page_url=page, conversation=conversation)}
    if client_obj and client_obj.enable_bookings:
        handlers['create_booking'] = _booking_handler(request, client_obj)
    if client_obj is not None:
        handlers['request_human'] = _human_handler(
            request, client_obj, conversation=conversation, page_url=page)

    language = client_obj.primary_language if client_obj else 'es'
    try:
        reply, usage = agent.answer(
            history, business_prompt=business_prompt, handlers=handlers,
            language=language)
    except agent.AgentNotConfigured:
        return JsonResponse(
            {'error': 'El asistente no está disponible ahora mismo.'}, status=503
        )
    except anthropic.RateLimitError:
        return JsonResponse(
            {'error': 'Estamos recibiendo muchas preguntas ahora mismo — trata de nuevo en un momento.'},
            status=429,
        )
    except anthropic.APIError:
        logger.exception('Anthropic API error in chat_api')
        return JsonResponse(
            {'error': 'Algo salió mal al conectar con el asistente. Trata de nuevo o usa el formulario de contacto.'},
            status=502,
        )

    # M-02/M-03/M-19: store the turns, cost and config fingerprint. The widget
    # sends the FULL history each turn, so rewriting the messages is a correct,
    # simple upsert (conversations are capped at a few turns anyway). Handler
    # side-effects (state, lead) were written with .update(), so nothing here
    # can clobber them. Best-effort: a persistence failure must not lose the reply.
    if conversation is not None:
        try:
            with transaction.atomic():
                # Append only — never rewrite the stored transcript from the
                # request body, or the record becomes whatever the last caller
                # claimed it was.
                next_pos = (conversation.chat_messages.aggregate(
                    m=Max('position'))['m'] or -1) + 1
                ChatMessage.objects.bulk_create([
                    ChatMessage(conversation=conversation, role='user',
                                content=history[-1]['content'][:MAX_MESSAGE_CHARS],
                                position=next_pos),
                    ChatMessage(conversation=conversation, role='assistant',
                                content=reply[:MAX_MESSAGE_CHARS],
                                position=next_pos + 1),
                ])
                Conversation.objects.filter(pk=conversation.pk).update(
                    tokens_used=F('tokens_used')
                    + usage['input_tokens'] + usage['output_tokens'],
                    last_message_at=timezone.now(),
                )
        except Exception:
            logger.exception('Failed to persist conversation %s', conversation.pk)

    return JsonResponse({'reply': reply})


@csrf_exempt
@_rate_limited('demo', limit=15, window=60)
def demo_api(request):
    """Public 'try your own agent' preview. Caller pastes their business context;
    the agent answers using it (ephemeral, nothing saved, no lead capture). Shares
    the same daily budget cap and rate limits as the live chat to bound cost."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    if len(request.body) > MAX_CHAT_BODY_BYTES * 2:
        return JsonResponse({'error': 'Demasiado texto. Recórtalo un poco.'}, status=413)
    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'Solicitud inválida.'}, status=400)

    context = (payload.get('context') or '').strip()[:6000]
    if not context:
        return JsonResponse({'error': 'Añade unas líneas sobre tu negocio primero.'}, status=400)

    history = []
    for item in (payload.get('messages') or []):
        if (isinstance(item, dict) and item.get('role') in ('user', 'assistant')
                and isinstance(item.get('content'), str)):
            c = item['content'].strip()[:MAX_MESSAGE_CHARS]
            if c:
                history.append({'role': item['role'], 'content': c})
    history = history[-getattr(settings, 'DOMINIO_AGENT_MAX_TURNS', 12):]
    while history and history[0]['role'] != 'user':
        history.pop(0)
    if not history or history[-1]['role'] != 'user':
        return JsonResponse({'error': 'No hay pregunta que contestar.'}, status=400)

    # The demo gets its OWN budget, separate from the tenants'. Sharing
    # 'chat:global:' meant an anonymous visitor hammering this public endpoint
    # could exhaust the ceiling and take every paying client's agent offline
    # for the rest of the day — on DOMINIO's own token bill.
    today = timezone.now().strftime('%Y%m%d')
    ip_cap = getattr(settings, 'DOMINIO_DEMO_IP_DAILY_CAP', 20)
    ip_key = f'demoip:{_client_ip(request)}:{today}'
    if cache.get(ip_key, 0) >= ip_cap:
        return JsonResponse(
            {'error': 'Llegaste al límite del demo por hoy. Escríbenos y te montamos uno real.'},
            status=429)

    daily_cap = getattr(settings, 'DOMINIO_DEMO_DAILY_CAP', 150)
    day_key = 'demo:global:' + today
    used = cache.get(day_key, 0)
    if used >= daily_cap:
        return JsonResponse({'error': 'El demo está ocupado ahora mismo. Trata más tarde.'}, status=503)
    for key in (day_key, ip_key):
        if cache.get(key, 0) == 0:
            cache.set(key, 1, 60 * 60 * 26)
        else:
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, 60 * 60 * 26)

    try:
        reply, _usage = agent.answer(history, business_prompt=context, handlers={})
    except agent.AgentNotConfigured:
        return JsonResponse({'error': 'El demo no está disponible ahora mismo.'}, status=503)
    except anthropic.RateLimitError:
        return JsonResponse({'error': 'Ocupado — trata de nuevo en un momento.'}, status=429)
    except anthropic.APIError:
        logger.exception('Anthropic API error in demo_api')
        return JsonResponse({'error': 'Algo salió mal. Trata de nuevo.'}, status=502)

    return JsonResponse({'reply': reply})


@csrf_exempt
@_cors_enabled
@_rate_limited('survey', limit=10, window=60)
def survey_api(request):
    """M-18: one optional CSAT rating per conversation, sent by the widget.
    Identified by (client, session) — the same pair that keyed the conversation."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'Solicitud inválida.'}, status=400)

    slug = payload.get('client')
    slug = slug.strip()[:60] if isinstance(slug, str) else 'dominio'
    session = payload.get('session')
    session = (''.join(c for c in session if c.isalnum() or c in '-_')[:64]
               if isinstance(session, str) else '')
    try:
        score = int(payload.get('score'))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Puntuación inválida.'}, status=400)
    if not (1 <= score <= 5) or len(session) < 8:
        return JsonResponse({'error': 'Puntuación inválida.'}, status=400)

    conversation = Conversation.objects.filter(
        client__slug=slug, widget_session=session).first()
    if conversation is None:
        return JsonResponse({'error': 'Conversación no encontrada.'}, status=404)

    # Same tenant credential as chat_api: without it, a guessed slug + session
    # lets anyone write a CSAT score into another business's dashboard.
    if conversation.client.slug != 'dominio':
        presented = payload.get('token')
        presented = presented.strip()[:64] if isinstance(presented, str) else ''
        expected = conversation.client.widget_token
        if not expected or not hmac.compare_digest(presented, expected):
            return JsonResponse({'error': 'Agente no autorizado.'}, status=403)

    _survey, created = Survey.objects.get_or_create(
        conversation=conversation,
        defaults={'score': score,
                  'comment': str(payload.get('comment') or '')[:1000]})
    if not created:
        return JsonResponse({'error': 'Esta conversación ya fue puntuada.'}, status=409)
    return JsonResponse({'ok': True})


# ============================================================
# GET STARTED — public pricing + agent signup funnel (lean self-serve)
# ============================================================

# Launch pricing (ago-2026). `setup` is the one-time "instalación y configuración"
# fee charged IN THE SAME CHECKOUT as the first period (Stripe one-time price
# alongside the recurring one). Annual = 10x monthly ("2 meses gratis").
# Custom has no self-serve checkout — it's a conversation.
PLANS = [
    {'id': 'starter', 'name': 'Starter', 'monthly': 99, 'annual': 990, 'setup': 500,
     'tagline': 'Para empezar a capturar leads 24/7',
     'features': ['Agente de IA en tu página web', 'Contesta las preguntas de tus clientes 24/7',
                  'Convierte visitantes en leads — directo a tu inbox',
                  'Dashboard privado para ver cada lead',
                  'Instalación y configuración por DOMINIO']},
    {'id': 'pro', 'name': 'Pro', 'monthly': 299, 'annual': 2990, 'setup': 1000, 'featured': True,
     'tagline': 'Leads calificados y citas automáticas',
     'features': ['Todo lo de Starter', 'Califica los leads antes de que te lleguen',
                  'Agenda citas y reservaciones automáticamente',
                  'Responde a los leads desde tu dashboard',
                  'Ajustado a tu marca y colores',
                  'Afinamos tu agente todos los meses']},
    {'id': 'scale', 'name': 'Scale', 'monthly': 499, 'annual': 4990, 'setup': 2500,
     'tagline': 'Varios locales o marcas',
     'features': ['Todo lo de Pro', 'WhatsApp y multicanal',
                  'Hasta 3 locales o marcas', 'Se conecta a tu CRM y calendario',
                  'Soporte prioritario']},
    {'id': 'custom', 'name': 'Custom', 'monthly': None, 'annual': None, 'setup': None,
     'tagline': 'Integraciones y flujos a la medida',
     'features': ['Todo lo de Scale', 'Locales y canales ilimitados',
                  'Integraciones y flujos a la medida', 'Onboarding dedicado']},
]
PLAN_BY_ID = {p['id']: p for p in PLANS}

# What each paid tier unlocks on the tenant at provisioning time (webhook).
# Kept next to PLANS so pricing copy and enforcement never drift apart.
PLAN_LIMITS = {
    'starter': {'daily_message_cap': 200, 'enable_bookings': False},
    'pro': {'daily_message_cap': 500, 'enable_bookings': True},
    'scale': {'daily_message_cap': 1500, 'enable_bookings': True},
    'custom': {'daily_message_cap': 3000, 'enable_bookings': True},
}


def _plan_name(plan):
    return PLAN_BY_ID.get(plan, {}).get('name') or (plan or 'Starter').title()


def _receipt(sub):
    """Summary of the first charge, for the printed receipt on /bienvenida/.

    Built from the plan table rather than from Stripe: the welcome page must
    render instantly and must not break when Stripe is slow. It is a summary,
    not the fiscal document — Stripe emails the real invoice — so the template
    says so. The setup fee is listed only when a Price id is configured for it,
    the same condition under which the checkout actually charged it.
    """
    plan = PLAN_BY_ID.get(sub.plan)
    if not plan or plan.get('monthly') is None:
        return None
    recurring = plan['annual'] if sub.period == 'annual' else plan['monthly']
    lines = [{
        'label': f"DOMINIO {plan['name']}",
        'detail': 'Un año' if sub.period == 'annual' else 'Un mes',
        'amount': recurring,
    }]
    if plan.get('setup') and payments.setup_price_id_for(sub.plan):
        lines.append({'label': 'Instalación y configuración',
                      'detail': 'Cargo único', 'amount': plan['setup']})
    money = lambda v: f'{v:,.2f}'      # no humanize app just for a thousands sep
    for li in lines:
        li['amount_str'] = money(li['amount'])
    pct = payments.tax_percent()
    discount = Decimal('0')

    if sub.initial_total_cents is not None:
        # What Stripe actually charged. Authoritative, and the only version that
        # survives a promotion code.
        cents = lambda v: Decimal(v or 0) / 100
        subtotal = cents(sub.initial_subtotal_cents)
        discount = cents(sub.initial_discount_cents)
        tax = cents(sub.initial_tax_cents)
        total = cents(sub.initial_total_cents)
    else:
        # Older rows (and ATH/manual) have no stored amounts: fall back to the
        # price table. Match Stripe exactly — tax each line, round HALF UP, then
        # add. Python's round() is banker's rounding on binary floats and leaves
        # $299 at 11.5% a cent under what the card was charged.
        subtotal = Decimal(sum(li['amount'] for li in lines))
        tax = Decimal('0')
        if pct:
            rate = Decimal(str(pct)) / 100
            for li in lines:
                tax += (Decimal(li['amount']) * rate).quantize(Decimal('0.01'), ROUND_HALF_UP)
        total = subtotal + tax

    return {
        'lines': lines,
        'subtotal': money(subtotal),
        'discount': money(discount) if discount else '',
        'tax_label': f'IVU {pct:g}%' if pct else '',
        'tax': money(tax) if (pct or tax) else '',
        'total': money(total),
        # Our order id when it exists; the Stripe session tail only as fallback
        # for rows created before order numbers existed.
        'ref': sub.order_number or (sub.checkout_session_id or '')[-8:].upper(),
        'date': sub.created_at,
        'period_label': 'Anual' if sub.period == 'annual' else 'Mensual',
    }


def _slug_for_company(company):
    """Public widget key for a new self-serve client: slugified company name,
    never 'dominio' (reserved for our own site), deduped with -2, -3..."""
    base = slugify(company or '')[:40] or 'cliente'
    if base == 'dominio':
        base = 'cliente'
    slug, n = base, 2
    while Client.objects.filter(slug=slug).exists():
        slug = f'{base}-{n}'
        n += 1
    return slug


def _normalize_website(raw):
    """'acme.com' / 'https://www.acme.com/x' -> a valid http(s) URL or ''."""
    raw = (raw or '').strip()[:200]
    if not raw:
        return ''
    if not raw.lower().startswith(('http://', 'https://')):
        raw = 'https://' + raw
    try:
        URLValidator(schemes=['http', 'https'])(raw)
    except ValidationError:
        return ''
    return raw


def _origins_from_website(website):
    """Comma-separated allowlist (host + www variant) derived from a site URL.
    Blank when nothing usable was given — the widget then runs anywhere."""
    url = _normalize_website(website)
    host = (urlparse(url).hostname or '').lower().strip('.') if url else ''
    if not host or '.' not in host:
        return ''
    bare = host[4:] if host.startswith('www.') else host
    return f'{bare}, www.{bare}'


def _stamp_order_number(sub):
    """Give the sale an id of ours (DOM-2026-0007), once and for good.

    Derived from the pk so it needs no counter and cannot collide; stored, not
    computed, so it survives a change of processor or of this format.
    """
    if sub is None or sub.order_number:
        return sub
    number = f'DOM-{(sub.created_at or timezone.now()).year}-{sub.pk:04d}'
    Subscription.objects.filter(pk=sub.pk).update(order_number=number)
    sub.order_number = number
    return sub


def _charged_from_session(obj):
    """What the card was really charged, straight off the Checkout Session.

    Deriving this from the price table instead is what made a $1.34 coupon
    purchase print a $667.89 receipt: the table cannot see a promotion code,
    and `allow_promotion_codes` is on for every checkout.
    """
    td = obj.get('total_details') or {}
    total = obj.get('amount_total')
    if total is None:
        return {}
    return {
        'initial_subtotal_cents': obj.get('amount_subtotal'),
        'initial_discount_cents': td.get('amount_discount') or 0,
        'initial_tax_cents': td.get('amount_tax') or 0,
        'initial_total_cents': total,
    }


def _provision_paid_client(request, *, email, plan, period, meta, cust_id, sub_id,
                           session_id='', charged=None, setup_fee_charged=False):
    """The self-serve alta: turn a paid Stripe checkout into a tenant.

    Three cases, keyed on the payer's email:
    - 'new': no Client with that email -> Client + Subscription + login;
    - 'link': a Client exists but is NOT an active Stripe tenant (hand-made,
      or a checkout that never activated) -> attach the subscription to it,
      make sure it has a login;
    - 'second': a Client exists AND already pays through Stripe -> a second
      business for the same owner. A brand-new tenant is created and the
      existing one is never touched (a new checkout can't relink or reactivate
      it). The login is shared: one login, many agents.
    The welcome (credentials + install link) and the ops alert go out in every
    case. Returns (client, created). DOMINIO installs the widget, so a new
    client starts 'pending' and staff flips it to 'live' from the factory.
    """
    plan = plan if plan in dict(Subscription.PLAN_CHOICES) else 'starter'
    period = 'annual' if period == 'annual' else 'monthly'
    sub_defaults = {
        'plan': plan, 'period': period, 'method': 'stripe', 'status': 'active',
        'stripe_customer_id': cust_id, 'stripe_subscription_id': sub_id,
        'checkout_session_id': (session_id or '')[:120],
        'setup_fee_charged': bool(setup_fee_charged),
    }
    sub_defaults.update({k: v for k, v in (charged or {}).items() if v is not None})

    meta = meta or {}
    company = _single_line(meta.get('company'), 160) or email.split('@')[0][:160]
    website = _normalize_website(meta.get('website'))
    contact_name = _single_line(meta.get('name'), 120)
    contact_phone = (meta.get('phone') or '').strip()[:30]
    lead = None
    try:
        lead = ContactSubmission.objects.filter(pk=int(meta.get('lead_id') or 0)).first()
    except (TypeError, ValueError):
        lead = None

    # Same checkout session delivered again under a NEW event id (the ledger
    # only catches identical ids): it is already provisioned — never a 2nd tenant.
    if session_id:
        done = Subscription.objects.filter(
            checkout_session_id=session_id[:120]).select_related('client').first()
        if done is not None:
            return done.client, False

    existing = Client.objects.filter(notify_email__iexact=email).first()
    existing_sub = Subscription.objects.filter(client=existing).first() if existing else None
    if existing is None:
        mode = 'new'
    elif (existing_sub is not None and existing_sub.stripe_subscription_id
            and existing_sub.status == 'active'):
        mode = 'second'
    else:
        mode = 'link'

    # Anyone can type someone else's address into the public signup form, so a
    # checkout email alone must not grant control of a tenant that already has
    # owners: relinking would repoint their billing portal at the payer's own
    # Stripe customer (exposing invoices and card details both ways) and let the
    # payer switch off the victim's agent by cancelling. Hand it to a human.
    if mode == 'link' and Membership.objects.filter(client=existing).exists():
        logger.warning('Checkout for %s targets tenant %s which already has owners',
                       email, existing.slug)
        _audit(request, 'stripe.link_refused', client=existing, result='blocked')
        ops = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
        if ops:
            _send_plain_email(
                f'[Stripe] REVISAR A MANO — pago apunta a {existing.slug}',
                [ops],
                (f'Un checkout pagado con {email} apunta al cliente existente '
                 f'"{existing.name}" ({existing.slug}), que ya tiene dueños. '
                 f'No se enlazó automáticamente para evitar que un tercero se '
                 f'apodere de la suscripción. Verifica quién pagó y enlaza a '
                 f'mano si es legítimo. Checkout: {session_id or "—"}'))
        return existing, False

    if mode == 'link':
        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['starter'])
        with transaction.atomic():
            _stamp_order_number(
                Subscription.objects.update_or_create(client=existing, defaults=sub_defaults)[0])
            # The tier they just paid for has to actually apply here too —
            # otherwise a linked client keeps whatever caps it happened to have.
            Client.objects.filter(pk=existing.pk).update(
                is_active=True,
                daily_message_cap=limits['daily_message_cap'],
                enable_bookings=limits['enable_bookings'])
            existing.is_active = True
            if lead is not None and lead.status != 'won':
                ContactSubmission.objects.filter(pk=lead.pk).update(status='won')
            username, temp_password = _provision_client_login(existing)
        client, created = existing, False
    else:
        prompt_lines = [f'Negocio: {company}']
        if website:
            prompt_lines.append(f'Sitio web: {website}')
        if contact_name or contact_phone:
            prompt_lines.append(
                f'Contacto: {contact_name or "—"}' + (f' ({contact_phone})' if contact_phone else ''))
        if lead is not None and lead.message.strip():
            prompt_lines.append('')
            prompt_lines.append(f'Lo que el cliente nos contó al registrarse:\n{lead.message.strip()}')
        prompt_lines.append('')
        prompt_lines.append('(Configuración inicial. DOMINIO completará el conocimiento del '
                            'negocio durante la instalación.)')
        limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['starter'])

        with transaction.atomic():
            client = None
            for attempt in range(3):
                try:
                    with transaction.atomic():
                        client = Client.objects.create(
                            name=company, slug=_slug_for_company(company),
                            system_prompt='\n'.join(prompt_lines),
                            notify_email=email, allowed_origins=_origins_from_website(website),
                            website_url=website, notify_phone=contact_phone,
                            daily_message_cap=limits['daily_message_cap'],
                            enable_bookings=limits['enable_bookings'],
                            is_active=True, onboarding_sent=True, setup_status='pending',
                            primary_language='es',
                        )
                    break
                except IntegrityError:
                    # Slug race with a concurrent create — recompute and retry.
                    if attempt == 2:
                        raise
            _stamp_order_number(
                Subscription.objects.update_or_create(client=client, defaults=sub_defaults)[0])
            if lead is not None and lead.status != 'won':
                ContactSubmission.objects.filter(pk=lead.pk).update(status='won')
            username, temp_password = _provision_client_login(client)
        created = True

        # Best-effort greeting from the seed knowledge (same as the factory form).
        try:
            greeting = agent.generate_greeting(client.system_prompt)
            if greeting:
                Client.objects.filter(pk=client.pk).update(greeting=greeting)
        except Exception:
            logger.exception('Greeting generation failed for %s', client.slug)

    dashboard_url = settings.SITE_URL + reverse('dashboard')
    login = ({'username': username, 'password': temp_password, 'url': dashboard_url}
             if username else None)
    embed = _embed_snippet(settings.SITE_URL, client.slug)
    install_url = settings.SITE_URL + reverse('install')

    # The temp password exists in exactly one place: this email. If it does not
    # send, the buyer has paid and cannot get in — so the result is checked, not
    # discarded, and the ops alert below is escalated.
    welcome_sent = _send_welcome_email(client, _plan_name(plan), login, install_url, embed,
                                       setup_fee_charged=bool(setup_fee_charged))
    if not welcome_sent:
        logger.error('Welcome email FAILED for %s <%s> — the customer cannot log in',
                     client.slug, email)
        _audit(request, 'welcome.email', client=client, result='failed')

    ops_email = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    if ops_email:
        factory_url = settings.SITE_URL + reverse('client_edit', args=[client.pk])
        login_note = ('' if login else
                      ' (sin acceso al dashboard: el email es una cuenta de staff)')
        if mode == 'second':
            subject = f'[Stripe] Segundo tenant para {email} — {client.name} — {plan}/{period}'
            intro = (f'El email ya tiene el agente "{existing.name}" ({existing.slug}) '
                     f'pagando por Stripe (customer={existing_sub.stripe_customer_id}). '
                     f'Se creó un SEGUNDO tenant y el primero no se tocó; el mismo login '
                     f'administra los dos.')
        elif mode == 'link':
            subject = f'[Cliente vinculado] {client.name} — {plan}/{period} — instalar'
            intro = (f'Un cliente que ya existía en el Factory ({client.slug}) pagó por '
                     f'Stripe: se vinculó la suscripción y se activó el agente.')
        else:
            subject = f'[Nuevo cliente] {client.name} — {plan}/{period} — instalar'
            intro = 'Nuevo cliente provisionado automáticamente desde Stripe.'
        if not welcome_sent:
            # The buyer paid and has no way in. This must be impossible to miss.
            subject = '[ACCION REQUERIDA] ' + subject
            intro = ('*** EL CORREO DE BIENVENIDA NO SALIO. El cliente pagó y NO tiene '
                     'sus credenciales: contáctalo hoy, mándaselas a mano o pídele '
                     'que use "¿Olvidaste tu contraseña?". *** ') + intro
        _send_plain_email(
            subject=subject,
            to=[ops_email],
            body=(f'{intro}\n\n'
                  f'Negocio: {client.name}\nEmail: {email}{login_note}\n'
                  f'Contacto: {contact_name or "—"} {contact_phone}\n'
                  f'Sitio web: {website or client.website_url or "—"}\nPlan: {plan}/{period}\n'
                  f'Stripe: customer={cust_id} subscription={sub_id}\n\n'
                  f'Factory: {factory_url}\n'
                  f'Snippet: {embed}\n\n'
                  f'Siguiente paso: el cliente enviará los datos de acceso a su sitio '
                  f'desde {install_url}. Instala el widget y marca el agente en vivo.'),
            reply_to=[email])
    logger.info('Self-serve checkout %s -> client %s (pk=%s) mode=%s plan=%s/%s',
                _id_tail(session_id), client.slug, client.pk, mode, plan, period)
    return client, created


@_rate_limited('signup', limit=5, window=300)
def get_started(request):
    """Public pricing page + 'get your AI agent' request form."""
    plan_names = {p['id']: p['name'] for p in PLANS}
    if request.method == 'POST':
        if request.POST.get('hp'):  # honeypot
            return redirect('get_started')
        company = _single_line(request.POST.get('company'), 160)
        name = _single_line(request.POST.get('name'), 120)
        email = (request.POST.get('email') or '').strip()[:254]
        phone = (request.POST.get('phone') or '').strip()[:30]
        website = (request.POST.get('website_url') or '').strip()[:200]
        plan = (request.POST.get('plan') or '').strip()[:40]
        period = 'annual' if (request.POST.get('period') or '') == 'annual' else 'monthly'
        details = (request.POST.get('message') or '').strip()[:3000]

        errors = {}
        if not name:
            errors['name'] = 'Requerido.'
        if not company:
            errors['company'] = 'Requerido.'
        if plan not in PLAN_BY_ID:
            errors['plan'] = 'Escoge un plan para continuar.'
        # Email OR phone — a phone-only signup is fine, but a given phone must be valid.
        if email:
            try:
                validate_email(email)
            except ValidationError:
                errors['email'] = 'Escribe un email válido.'
        phone = normalize_phone(phone)
        if phone is None:
            errors['phone'] = 'Escribe un número de teléfono válido, ej. (787) 123-4567.'
            phone = ''
        if not email and not phone:
            errors['email'] = 'Déjanos un email o un teléfono para poder contactarte.'
        if errors:
            return render(request, 'landing/get_started.html',
                          {'plans': PLANS, 'form': request.POST, 'errors': errors})

        submission = ContactSubmission(
            client=Client.objects.filter(slug='dominio').first(),
            name=name, email=email, phone=phone, company=company, service='ai-automation',
            source='signup', status='new',
            message=(f'[AGENT SIGNUP — Plan: {plan_names.get(plan, plan or "—")}]\n'
                     f'Website: {website or "—"}\n\n{details}'),
            ip_address=_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:300],
            page_url=_page_url(request.META.get('HTTP_REFERER', '')),
        )
        submission.save()
        _send_lead_emails(submission)

        # M-01: when Stripe is configured for this plan, go straight to hosted
        # checkout (setup fee + first period in ONE session). The webhook then
        # provisions the tenant automatically — no manual coordination. Any
        # Stripe hiccup falls back to the email flow (the lead is already saved).
        # A self-serve plan that cannot reach checkout is a lost sale, and every
        # way it fails is invisible: a missing recurring price silently serves
        # the email flow, and a missing setup price silently drops the install
        # fee the page just advertised. Say so, loudly, to someone who can fix it.
        if email and plan in ('starter', 'pro', 'scale') and payments.stripe_enabled():
            missing = []
            if not payments.price_id_for(plan, period):
                missing.append(f'STRIPE_PRICE_{plan.upper()}_{period.upper()}')
            if PLAN_BY_ID.get(plan, {}).get('setup') and not payments.setup_price_id_for(plan):
                missing.append(f'STRIPE_PRICE_{plan.upper()}_SETUP')
            if missing:
                logger.error('Stripe price(s) not configured: %s — signup %s fell back '
                             'to the email flow', ', '.join(missing), submission.pk)
                ops = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
                if ops:
                    _send_plain_email(
                        subject=f'[ACCION REQUERIDA] Falta configurar precio de Stripe ({plan})',
                        to=[ops],
                        body=(f'Un cliente escogió {plan}/{period} pero estas variables de '
                              f'entorno no están puestas: {", ".join(missing)}.\n\n'
                              f'Consecuencia: '
                              + ('el checkout NO se abrió y el cliente cayó al flujo de '
                                 'correo. Escríbele hoy con el enlace de pago.\n\n'
                                 if not payments.price_id_for(plan, period) else
                                 'se cobró la mensualidad SIN el cargo de instalación que '
                                 'anuncia la página.\n\n')
                              + f'Cliente: {name} <{email}> — {company}\n'
                              + f'Corre "manage.py preflight --stripe" para ver todo.'))

        if email and payments.stripe_enabled() and payments.price_id_for(plan, period):
            try:
                session = payments.create_checkout_session(
                    plan=plan, period=period, email=email,
                    success_url=request.build_absolute_uri(reverse('bienvenida'))
                    + '?session_id={CHECKOUT_SESSION_ID}',
                    cancel_url=request.build_absolute_uri(
                        reverse('get_started')) + '#planes',
                    setup_price=payments.setup_price_id_for(plan),
                    metadata={
                        'company': company, 'name': name, 'phone': phone,
                        'website': website, 'lead_id': str(submission.pk),
                    },
                )
                if session.get('url'):
                    return redirect(session['url'])
            except payments.StripeError:
                logger.exception('Stripe checkout failed for signup %s', submission.pk)

        if email:
            messages.success(
                request,
                '¡Listo! Te escribimos por email en breve con el enlace de pago y los próximos pasos.')
        else:
            messages.success(
                request,
                '¡Listo! Te llamamos o te escribimos por WhatsApp/SMS en breve para montar '
                'tu agente y el pago.')
        return redirect(reverse('get_started') + '#done')

    return render(request, 'landing/get_started.html',
                  {'plans': PLANS, 'form': {}, 'errors': {}})


@csrf_exempt
def stripe_webhook(request):
    """M-01: signed, idempotent Stripe webhook. Activates a client's subscription
    on successful checkout and suspends the widget when payment dies. Always
    answers 2xx for verified events so Stripe stops retrying."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido.'}, status=405)
    payload = request.body
    if len(payload) > 256 * 1024:
        return JsonResponse({'error': 'Payload demasiado grande.'}, status=413)
    sig = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    if not payments.verify_webhook_signature(payload, sig):
        return JsonResponse({'error': 'Firma inválida.'}, status=400)

    try:
        event = json.loads(payload)
    except ValueError:
        return JsonResponse({'error': 'JSON inválido.'}, status=400)
    event_id = str(event.get('id') or '')[:120]
    event_type = str(event.get('type') or '')
    obj = (event.get('data') or {}).get('object') or {}

    # Idempotency: a retried delivery is acknowledged but never re-acted on.
    # The slot is only burned once the handler FINISHES (handled_at). A row
    # claimed but never stamped means the last attempt died mid-flight — worker
    # OOM, timeout, dropped connection — so Stripe's retry must run it again
    # rather than get a cheerful "duplicate" while the customer's money sits in
    # our account with nothing provisioned. Re-running is safe: provisioning
    # dedupes on Subscription.checkout_session_id.
    row = None
    if event_id:
        row, _fresh = ProcessedWebhookEvent.objects.get_or_create(event_id=event_id)
        if row.handled_at:
            return JsonResponse({'ok': True, 'duplicate': True})

    _handle_stripe_event(request, event_type, obj)

    if row is not None:
        ProcessedWebhookEvent.objects.filter(pk=row.pk).update(handled_at=timezone.now())

    return JsonResponse({'ok': True})


def widget_should_run(status):
    """Single source of truth for "does the money say this agent stays up?".

    'past_due' keeps running on purpose: Stripe is still retrying the card and
    dunning the customer, and cutting service off on the first failed retry
    punishes people whose card simply expired. Only a dead subscription stops it.

    The webhook and the nightly reconcile MUST agree here — when they disagreed,
    a customer's agent went down or came back depending on which ran last.
    """
    return status in ('active', 'past_due')


def _handle_stripe_event(request, event_type, obj):
    """Apply one verified, not-yet-seen Stripe event. Raises on unexpected
    failure so the caller can release the idempotency ledger."""
    ops_email = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')

    if event_type in ('checkout.session.completed',
                      'checkout.session.async_payment_succeeded'):
        email = ((obj.get('customer_details') or {}).get('email')
                 or obj.get('customer_email') or '').strip().lower()
        meta = obj.get('metadata') or {}
        plan = (meta.get('plan') or 'starter')[:20]
        period = 'annual' if meta.get('period') == 'annual' else 'monthly'
        sub_id = str(obj.get('subscription') or '')[:80]
        cust_id = str(obj.get('customer') or '')[:80]

        session_id = str(obj.get('id') or '')[:120]
        payment_status = obj.get('payment_status')

        if not email:
            _audit(request, 'stripe.checkout.completed', target=sub_id,
                   result='sin email del pagador')
            logger.error('Checkout %s completed without a payer email', _id_tail(session_id))
        elif payment_status not in (None, 'paid', 'no_payment_required'):
            # e.g. 'unpaid' (delayed payment methods): never provision, link or
            # (re)activate on credit, whether or not the tenant already exists.
            client_obj = Client.objects.filter(notify_email__iexact=email).first()
            _audit(request, 'stripe.checkout.completed', client=client_obj, target=sub_id,
                   result=f'no provisionado: payment_status={payment_status}')
            logger.warning('Checkout %s not paid yet (%s); skipped provisioning',
                           _id_tail(session_id), payment_status)
            if ops_email:
                _send_plain_email(
                    subject=f'[Stripe] Checkout sin pagar todavía ({email})',
                    to=[ops_email],
                    body=(f'Un checkout quedó en payment_status={payment_status}, '
                          f'así que NO se provisionó nada.\n\n'
                          f'Si el pago llega después, Stripe manda '
                          f'checkout.session.async_payment_succeeded y el alta corre '
                          f'sola. Si no llega en 24h, este cliente no tiene agente: '
                          f'contáctalo.\n\n'
                          f'Email: {email}\nPlan: {plan}/{period}\n'
                          f'Checkout: {session_id}'))
        else:
            # The self-serve alta. _provision_paid_client decides between a new
            # tenant, linking a pre-created one, or a second tenant for an
            # owner who already pays through Stripe (never touching the first).
            try:
                new_client, created = _provision_paid_client(
                    request, email=email, plan=plan, period=period, meta=meta,
                    cust_id=cust_id, sub_id=sub_id, session_id=session_id,
                    charged=_charged_from_session(obj),
                    setup_fee_charged=bool(payments.setup_price_id_for(plan)))
                _audit(request, 'stripe.checkout.completed', client=new_client,
                       target=sub_id,
                       result=f'{plan}/{period} ' + ('provisionado' if created else 'vinculado'))
            except Exception:
                logger.exception('Auto-provisioning failed for checkout %s',
                                 _id_tail(session_id))
                _audit(request, 'stripe.checkout.completed', target=sub_id,
                       result=f'ERROR provisionando {email}')
                if ops_email:
                    _send_plain_email(
                        subject=f'[Stripe] Pago recibido — provisionar agente A MANO ({email})',
                        to=[ops_email],
                        body=(f'El alta automática falló (ver logs).\n'
                              f'Email: {email}\nPlan: {plan}/{period}\n'
                              f'Subscription: {sub_id}\nCheckout: {session_id}\n'
                              f'Metadata: {json.dumps(meta, ensure_ascii=False)}\n\n'
                              f'Crea el agente en el Factory con notify_email={email}.'))

    elif event_type in ('charge.refunded', 'charge.dispute.created'):
        # Money left the account. Until now nobody listened for these, so a
        # refunded customer — or one who charged back — kept a working agent
        # forever, and the only trace was in Stripe.
        obj_cust = str(obj.get('customer') or '')[:80]
        # A dispute wraps the charge; a refund IS the charge.
        if event_type == 'charge.dispute.created' and not obj_cust:
            charge = obj.get('charge')
            if isinstance(charge, dict):
                obj_cust = str(charge.get('customer') or '')[:80]
        sub = (Subscription.objects.filter(stripe_customer_id=obj_cust)
               .select_related('client').first()) if obj_cust else None
        if sub is None:
            logger.warning('%s for unknown customer %s', event_type, _id_tail(obj_cust))
            return
        disputed = event_type == 'charge.dispute.created'
        # A partial refund is a goodwill gesture, not the end of the relationship
        # — only a full one stops the service.
        amount = obj.get('amount') or 0
        full = disputed or (obj.get('amount_refunded') or 0) >= amount > 0
        _audit(request, f'stripe.{event_type.rsplit(".", 1)[-1]}', client=sub.client,
               target=sub.stripe_subscription_id,
               result='disputa' if disputed else ('reembolso total' if full
                                                  else 'reembolso parcial'))
        if not full:
            if ops_email:
                _send_plain_email(
                    subject=f'[Stripe] Reembolso parcial — {sub.client.slug}',
                    to=[ops_email],
                    body=(f'Se reembolsaron ${(obj.get("amount_refunded") or 0)/100:,.2f} '
                          f'de ${amount/100:,.2f} a {sub.client.name} '
                          f'({sub.order_number or sub.stripe_subscription_id}).\n\n'
                          f'El agente sigue activo: un reembolso parcial no corta '
                          f'el servicio. Si debe cortarse, hazlo a mano.'))
            return
        Subscription.objects.filter(pk=sub.pk).update(status='canceled')
        Client.objects.filter(pk=sub.client_id).update(is_active=False)
        if ops_email:
            _send_plain_email(
                subject=(f'[Stripe] {"DISPUTA" if disputed else "Reembolso"} — '
                         f'{sub.client.slug}'),
                to=[ops_email],
                body=(f'{"Un chargeback" if disputed else "Un reembolso total"} entro '
                      f'para {sub.client.name} '
                      f'({sub.order_number or sub.stripe_subscription_id}).\n\n'
                      f'El widget quedo pausado y la suscripcion marcada cancelada '
                      f'AQUI.\n\n'
                      f'*** OJO: en Stripe la suscripcion SIGUE VIVA y volvera a '
                      f'cobrar en la proxima fecha de renovacion. ***\n'
                      f'Esto no se cancela solo a proposito: un reembolso de buena '
                      f'voluntad de un mes no debe matarle la cuenta a un cliente '
                      f'que se queda. Si esta venta se deshace, cancelala tu en:\n'
                      f'https://dashboard.stripe.com/subscriptions/'
                      f'{sub.stripe_subscription_id}\n'
                      + ('\nResponde la disputa en Stripe con la evidencia: orden, '
                         'pago, usuario, fecha y servicio entregado. Tienes dias '
                         'contados.\n' if disputed else '')
                      + f'\nCliente: {sub.client.notify_email}\nPlan: {sub.plan}/{sub.period}'))

    elif event_type in ('customer.subscription.updated', 'customer.subscription.deleted'):
        sub_id = str(obj.get('id') or '')[:80]
        stripe_status = obj.get('status') or ''
        items = ((obj.get('items') or {}).get('data') or [])
        # Stripe moved current_period_end onto the items in the 2025-03-31
        # ('basil') API version. Read the legacy top level first, then the item.
        period_end = obj.get('current_period_end') or (
            items[0].get('current_period_end') if items else None)
        sub = Subscription.objects.filter(stripe_subscription_id=sub_id).select_related('client').first()
        if sub is not None:
            if event_type == 'customer.subscription.deleted' or stripe_status == 'canceled':
                new_status = 'canceled'
            elif stripe_status in ('active', 'trialing'):
                new_status = 'active'
            elif stripe_status in ('past_due', 'unpaid', 'incomplete', 'incomplete_expired'):
                new_status = 'past_due'
            else:
                new_status = sub.status
            updates = {'status': new_status}
            # A plan change arrives as an 'updated' event carrying the new
            # price. Mirror it locally, or we bill the new tier while serving
            # the old one's limits (and show the customer a stale plan name).
            price_id = ((items[0].get('price') or {}).get('id') or '') if items else ''
            if price_id:
                configured = getattr(settings, 'STRIPE_PRICES', {})
                match = next((k for k, v in configured.items()
                              if v == price_id and not k.endswith(':setup')), '')
                if match:
                    new_plan, new_period = match.split(':', 1)
                    if new_plan != sub.plan or new_period != sub.period:
                        limits = PLAN_LIMITS.get(new_plan, PLAN_LIMITS['starter'])
                        updates['plan'] = new_plan
                        updates['period'] = new_period
                        Client.objects.filter(pk=sub.client_id).update(
                            daily_message_cap=limits['daily_message_cap'],
                            enable_bookings=limits['enable_bookings'])
                        logger.info('Plan change for %s: %s/%s -> %s/%s',
                                    sub.client.slug, sub.plan, sub.period,
                                    new_plan, new_period)
            if period_end:
                try:
                    import datetime as _dt
                    updates['current_period_end'] = _dt.datetime.fromtimestamp(
                        int(period_end), tz=_dt.timezone.utc)
                except (ValueError, TypeError, OSError):
                    pass
            Subscription.objects.filter(pk=sub.pk).update(**updates)
            # Suspend the widget when the subscription dies; reactivate on recovery.
            if not widget_should_run(new_status) or stripe_status == 'unpaid':
                Client.objects.filter(pk=sub.client_id).update(is_active=False)
                if ops_email:
                    _send_plain_email(
                        subject=f'[Stripe] Suscripción caída — {sub.client.slug}',
                        to=[ops_email],
                        body=(f'La suscripción {sub_id} de {sub.client.name} quedó '
                              f'"{stripe_status}". El widget fue pausado.'))
            elif new_status == 'active' and not sub.client.is_active:
                Client.objects.filter(pk=sub.client_id).update(is_active=True)
            _audit(request, f'stripe.{event_type.rsplit(".", 1)[-1]}',
                   client=sub.client, target=sub_id, result=new_status)


_SESSION_ID_RE = re.compile(r'^cs_[A-Za-z0-9_]{1,117}$')


BIENVENIDA_MAX_POLLS = 20


@_rate_limited('bienvenida', limit=60, window=300, methods=('GET', 'POST'))
def bienvenida(request):
    """Post-checkout welcome page. The session id in the URL is never trusted:
    a session the webhook already turned into a Subscription is answered from
    the DB; otherwise Stripe is asked server-side and nothing is shown unless
    it says 'paid'. The (minimal) Stripe answer is cached 10 min per session
    because the page polls until the webhook has created the tenant (`poll`,
    at most BIENVENIDA_MAX_POLLS times); Stripe errors are cached 60 s so
    retries don't pile up 15 s calls."""
    session_id = (request.GET.get('session_id') or '').strip()
    try:
        n = int(request.GET.get('n') or 0)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(n, 30))
    context = {'paid': False, 'email': '', 'client': None, 'plan_name': '',
               'dashboard_url': settings.SITE_URL + reverse('dashboard'),
               'install_url': settings.SITE_URL + reverse('install'),
               'poll': False, 'poll_url': '', 'error': '', 'receipt': None}
    error_msg = ('No pudimos confirmar el pago. Si ya pagaste, revisa tu email — '
                 'te escribimos con los próximos pasos.')
    if not _SESSION_ID_RE.match(session_id):
        context['error'] = error_msg
        return render(request, 'landing/bienvenida.html', context)

    # Fast path: the webhook already provisioned this checkout -> no Stripe call.
    known = (Subscription.objects.filter(checkout_session_id=session_id)
             .select_related('client').first())
    if known is not None:
        context.update({
            'paid': True, 'email': known.client.notify_email, 'client': known.client,
            'plan_name': _plan_name(known.plan), 'poll': False,
            'receipt': _receipt(known),
        })
        return render(request, 'landing/bienvenida.html', context)

    if not payments.stripe_enabled():
        context['error'] = error_msg
        return render(request, 'landing/bienvenida.html', context)

    cache_key = f'cs:{session_id}'
    session = cache.get(cache_key)
    if session is None:
        try:
            raw = payments.retrieve_checkout_session(session_id)
        except payments.StripeError:
            logger.exception('Could not retrieve checkout session %s', _id_tail(session_id))
            cache.set(cache_key, {'payment_status': 'error', 'email': '', 'plan': ''}, 60)
            context['error'] = error_msg
            return render(request, 'landing/bienvenida.html', context)
        # Cache only what the page needs, never the whole Stripe object (PII).
        session = {
            'payment_status': raw.get('payment_status'),
            'email': ((raw.get('customer_details') or {}).get('email')
                      or raw.get('customer_email') or '').strip().lower(),
            'plan': ((raw.get('metadata') or {}).get('plan') or 'starter')[:20],
        }
        cache.set(cache_key, session, 600)

    paid = session.get('payment_status') == 'paid'
    if not paid:
        context['error'] = error_msg
        return render(request, 'landing/bienvenida.html', context)

    email = session.get('email') or ''
    plan = session.get('plan') or 'starter'
    client_obj = Client.objects.filter(notify_email__iexact=email).first() if email else None
    poll = client_obj is None and n < BIENVENIDA_MAX_POLLS
    context.update({
        'paid': True, 'email': email, 'client': client_obj,
        'plan_name': _plan_name(plan), 'poll': poll,
    })
    if poll:
        context['poll_url'] = f"{reverse('bienvenida')}?session_id={session_id}&n={n + 1}"
    elif client_obj is None:
        context['error'] = ('El pago se confirmó pero tu cuenta tarda más de lo normal — '
                            'escríbenos a hola@dominiopr.com y lo resolvemos hoy.')
    return render(request, 'landing/bienvenida.html', context)


def terms(request):
    return render(request, 'landing/terms.html')


def privacy(request):
    return render(request, 'landing/privacy.html')


# ============================================================
# DASHBOARD — branded internal backend to view & manage leads
# ============================================================

@login_required
def dashboard(request):
    """Branded leads dashboard: stats + filterable, searchable table.

    Scoped per tenant: a client sees only their own leads, DOMINIO staff see all.
    (The temp-password gate for every /dashboard/ page lives in
    landing.middleware.TempPasswordGateMiddleware.)
    """
    scoped = leads_for(request.user)
    qs = scoped

    # Filters (from querystring)
    source = request.GET.get('source', '')
    status = request.GET.get('status', '')
    q = request.GET.get('q', '').strip()

    if source in dict(ContactSubmission.SOURCE_CHOICES):
        qs = qs.filter(source=source)
    if status in dict(ContactSubmission.STATUS_CHOICES):
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(email__icontains=q)
            | Q(phone__icontains=q)
            | Q(company__icontains=q)
            | Q(message__icontains=q)
        )

    # Stats over the SAME scoped queryset — never the whole table.
    month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stats = {
        'total': scoped.count(),
        'new': scoped.filter(status='new').count(),
        'this_month': scoped.filter(created_at__gte=month_start).count(),
        'from_chat': scoped.filter(source='chat').count(),
    }

    context = {
        'leads': qs[:300],
        'stats': stats,
        'status_choices': ContactSubmission.STATUS_CHOICES,
        'source_choices': ContactSubmission.SOURCE_CHOICES,
        'cur_source': source,
        'cur_status': status,
        'q': q,
        'default_from': settings.DEFAULT_FROM_EMAIL,
        'is_staff': request.user.is_staff,
    }
    return render(request, 'landing/dashboard.html', context)


@login_required
@require_POST
def lead_status(request, pk):
    """Update a single lead's status, then return to the dashboard."""
    lead = get_lead_or_404(request.user, pk)
    new_status = request.POST.get('status', '')
    if new_status in dict(ContactSubmission.STATUS_CHOICES):
        lead.status = new_status
        lead.save(update_fields=['status'])
    # Return to the referring URL, but only if it's local (no open redirect).
    ref = request.META.get('HTTP_REFERER', '')
    if not url_has_allowed_host_and_scheme(ref, allowed_hosts={request.get_host()}):
        ref = reverse('dashboard')
    return redirect(ref)


@login_required
@require_POST
def lead_email(request, pk):
    """Send a one-off email to a lead straight from the dashboard.

    The whole point of the leads view is to act on a lead without leaving it.
    Replies route through the same Gmail SMTP the rest of the site uses, with
    Reply-To set to the owning tenant's inbox so the lead's answer lands there.
    A successful send nudges a brand-new lead to 'contacted' so the pipeline
    reflects reality.
    """
    lead = get_lead_or_404(request.user, pk)
    subject = (request.POST.get('subject') or '').strip()[:200]
    body = (request.POST.get('body') or '').strip()[:10000]
    # Stay where the user was (same safe-referer pattern as lead_status).
    ref = request.META.get('HTTP_REFERER', '')
    if not url_has_allowed_host_and_scheme(ref, allowed_hosts={request.get_host()}):
        ref = reverse('dashboard')

    # Phone-only leads have no email to reply to — never send to an empty address.
    if not lead.email:
        messages.error(request, 'Este lead no tiene email — contáctalo por teléfono.')
        return redirect(ref)

    if not subject or not body:
        messages.error(request, 'El asunto y el mensaje son requeridos.')
        return redirect(ref)

    # Per-user daily cap: every reply goes out through DOMINIO's single shared
    # Gmail (reputation + ~500/day quota). One tenant must not be able to burn it.
    cap_key = f'mailcap:{request.user.pk}'
    if cache.get(cap_key, 0) >= 50:
        messages.error(request, 'Llegaste al límite de emails de hoy. Trata de nuevo mañana.')
        return redirect(ref)

    business = lead.client.name if lead.client else 'DOMINIO'
    reply_to = (lead.client.notify_email if lead.client and lead.client.notify_email
                else getattr(settings, 'CONTACT_NOTIFY_EMAIL', ''))
    sent = _send_html_email(
        subject=subject,
        to=[lead.email],
        html_template='landing/emails/manual_message.html',
        txt_template='landing/emails/manual_message.txt',
        context={'body': body, 'lead': lead, 'business': business},
        reply_to=[reply_to] if reply_to else None,
    )
    if sent:
        cache.set(cap_key, cache.get(cap_key, 0) + 1, 86400)
        if lead.status == 'new':
            lead.status = 'contacted'
            lead.save(update_fields=['status'])
        messages.success(request, f'Email enviado a {lead.email}.')
    else:
        messages.error(
            request,
            f'No se pudo enviar el email a {lead.email}. Revisa la configuración de correo y trata de nuevo.')
    return redirect(ref)


@login_required
@_rate_limited('pwchange', limit=10, window=300)
def password_change(request):
    """Let any dashboard user (client or staff) change their own password —
    the onboarding email tells clients to do this after first sign-in.
    Rate-limited so the current-password check can't be hammered."""
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)  # stay logged in
            # Clear the first-login gate across all of the user's agents.
            Membership.objects.filter(user=request.user, must_change_password=True) \
                .update(must_change_password=False)
            messages.success(request, 'Tu contraseña se actualizó.')
            return redirect('dashboard')
    else:
        form = PasswordChangeForm(request.user)
    return render(request, 'landing/dashboard_password.html', {'form': form})


class DashboardPasswordResetConfirmView(PasswordResetConfirmView):
    """Django's confirm view + clears the first-login gate: a user who set
    their own password via reset no longer needs to change the temp one."""
    template_name = 'landing/dashboard_password_reset_confirm.html'
    success_url = reverse_lazy('password_reset_complete')

    def form_valid(self, form):
        response = super().form_valid(form)
        Membership.objects.filter(user=self.user, must_change_password=True) \
            .update(must_change_password=False)
        return response


# ============================================================
# CLIENT SELF-SERVICE — install details + billing
# ============================================================

def _is_org_admin(user, client):
    if user.is_staff:
        return True
    return Membership.objects.filter(user=user, client=client, role='org_admin').exists()


def _own_client(request):
    """The ONE organization a self-service page is about. Members: one of
    their clients (`?client=<slug>` picks among several). Staff: any client by
    slug, else their own memberships. None = nothing to show (caller decides).
    Scope is applied BEFORE the slug lookup — a cross-tenant slug yields None."""
    slug = (request.GET.get('client') or request.POST.get('client') or '').strip()[:60]
    ids = _member_client_ids(request.user)
    qs = Client.objects.filter(id__in=ids) if ids else Client.objects.none()
    if request.user.is_staff and slug:
        qs = Client.objects.all()
    if slug:
        return qs.filter(slug=slug).first()
    return qs.order_by('pk').first()


def _no_client_response(request):
    if request.user.is_staff:
        messages.info(request, 'Escoge un agente de la fábrica para ver su instalación o facturación.')
        return redirect('clients_list')
    raise Http404


@login_required
@_rate_limited('install', limit=20, window=300)
def install(request):
    """Client-facing 'how do we get into your site' page. DOMINIO installs
    the widget (done-for-you); the client can also paste the snippet themselves."""
    client = _own_client(request)
    if client is None:
        return _no_client_response(request)
    embed = _embed_snippet(request.build_absolute_uri('/'), client.slug)
    # Prefill from what we already have: a returning customer must see their own
    # answers, not empty boxes that overwrite them on submit.
    form, errors, saved = {
        'website_url': client.website_url,
        'platform': client.platform,
        'install_notes': client.install_notes,
        'mode': 'dominio',
    }, {}, False

    if request.method == 'POST':
        if not _is_org_admin(request.user, client):
            return HttpResponseForbidden(
                'Solo un administrador puede enviar los datos de instalación.')
        form = {
            'website_url': (request.POST.get('website_url') or '').strip()[:200],
            'platform': (request.POST.get('platform') or '').strip()[:40],
            'mode': (request.POST.get('mode') or 'dominio').strip()[:10],
            'install_notes': (request.POST.get('install_notes') or '').strip()[:3000],
        }
        website = _normalize_website(form['website_url'])
        if form['website_url'] and not website:
            errors['website_url'] = 'Escribe la dirección completa de tu sitio, ej. https://tunegocio.com'
        if form['platform'] and form['platform'] not in dict(Client.PLATFORM_CHOICES):
            errors['platform'] = 'Escoge una plataforma de la lista.'
        if form['mode'] not in ('dominio', 'yo'):
            errors['mode'] = 'Escoge una opción.'
        if form['mode'] == 'dominio' and not form['install_notes'] and not errors:
            errors['install_notes'] = ('Cuéntanos cómo entrar a tu sitio (o con quién '
                                       'coordinar) para instalar el agente.')
        if not errors:
            # Never let a blank field erase a stored value — the access notes in
            # particular may be the only copy, and staff may not have read them yet.
            client.website_url = website or client.website_url
            client.platform = form['platform'] or client.platform
            client.install_notes = form['install_notes'] or client.install_notes
            if website and not client.allowed_origins:
                client.allowed_origins = _origins_from_website(website)
            client.save(update_fields=['website_url', 'platform', 'install_notes',
                                       'allowed_origins'])
            _audit(request, 'install.details', client=client,
                   target=client.slug, result=form['mode'])
            if form['mode'] == 'dominio':
                ops_email = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
                if ops_email:
                    _send_plain_email(
                        subject=f'[Instalación] {client.name} envió datos de acceso',
                        to=[ops_email],
                        body=(f'Cliente: {client.name} ({client.slug})\n'
                              f'Email: {client.notify_email}\n'
                              f'Sitio: {website or "—"}\n'
                              f'Plataforma: {dict(Client.PLATFORM_CHOICES).get(form["platform"], "—")}\n\n'
                              f'Notas de acceso: ver en el Factory (no viajan por email).\n\n'
                              f'Factory: {settings.SITE_URL}{reverse("client_edit", args=[client.pk])}\n'
                              f'Snippet: {embed}'),
                        reply_to=[client.notify_email])
                messages.success(request, 'Recibido. Instalamos tu agente y te avisamos por email cuando esté en vivo.')
            else:
                messages.success(request, 'Guardado. Pega el snippet en tu sitio y avísanos cuando esté listo.')
            saved = True
            form = {}
        else:
            messages.error(request, 'Revisa los campos marcados y trata de nuevo.')

    return render(request, 'landing/dashboard_install.html', {
        'active_tab': 'install', 'active': 'install',
        'client': client, 'embed': embed, 'form': form, 'errors': errors,
        'platforms': Client.PLATFORM_CHOICES, 'status': client.setup_status,
        'saved': saved,
    })


@login_required
def billing(request):
    client = _own_client(request)
    if client is None:
        return _no_client_response(request)
    subscription = Subscription.objects.filter(client=client).first()
    plan = PLAN_BY_ID.get(subscription.plan) if subscription else None
    return render(request, 'landing/dashboard_billing.html', {
        'active_tab': 'billing', 'active': 'billing',
        'client': client, 'subscription': subscription, 'plan': plan,
        'plan_name': _plan_name(subscription.plan) if subscription else '',
        'portal_available': bool(
            subscription and payments.stripe_enabled() and subscription.stripe_customer_id
            and _is_org_admin(request.user, client)),
    })


@login_required
@require_POST
@_rate_limited('portal', limit=10, window=300)
def billing_portal(request):
    """Hand the client to Stripe's Customer Portal (invoices, card, cancel)."""
    client = _own_client(request)
    if client is None:
        return _no_client_response(request)
    if not _is_org_admin(request.user, client):
        messages.error(request, 'Solo un administrador puede gestionar la facturación.')
        return redirect('billing')
    subscription = Subscription.objects.filter(client=client).first()
    if not (subscription and payments.stripe_enabled() and subscription.stripe_customer_id):
        messages.error(request, 'La facturación de esta cuenta no se maneja por Stripe. Escríbenos y te ayudamos.')
        return redirect('billing')
    try:
        session = payments.create_portal_session(
            subscription.stripe_customer_id,
            return_url=request.build_absolute_uri(reverse('billing')))
    except payments.StripeError:
        logger.exception('Portal session failed for %s', client.slug)
        _audit(request, 'billing.portal', client=client, target=client.slug, result='error')
        messages.error(request, 'No pudimos abrir el portal de facturación. Trata de nuevo en un momento.')
        return redirect('billing')
    _audit(request, 'billing.portal', client=client, target=client.slug)
    if not session.get('url'):
        messages.error(request, 'No pudimos abrir el portal de facturación. Trata de nuevo en un momento.')
        return redirect('billing')
    return redirect(session['url'])


# ============================================================
# AGENT FACTORY — embeddable widget + client management
# ============================================================

def _hex_rgb(hex_color, fallback=(0x34, 0xd6, 0xc8)):
    """Parse a 3/6-digit hex (with/without #) to an (r, g, b) tuple."""
    raw = (hex_color or '').strip().lstrip('#')
    if len(raw) == 3:
        raw = ''.join(c * 2 for c in raw)
    try:
        return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return fallback


def _luminance(rgb):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])


def _mix(rgb, target, t):
    return tuple(round(c + (tc - c) * t) for c, tc in zip(rgb, target))


def _hexs(rgb):
    return '#%02x%02x%02x' % tuple(rgb)


def _contrast(a, b):
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _on(rgb, dark=(15, 31, 46), light=(255, 255, 255)):
    """Pick the text color (near-black or white) with the MOST contrast on `rgb`
    — measured, not by a luminance threshold, so mid-tones never fall short."""
    return dark if _contrast(dark, rgb) >= _contrast(light, rgb) else light


def _rgb_hsl(rgb):
    r, g, b = (c / 255 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        return (0.0, 0.0, l)
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = (g - b) / d + (6 if g < b else 0)
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return (h / 6, s, l)


def _hsl_rgb(hsl):
    h, s, l = hsl
    if s == 0:
        v = round(l * 255)
        return (v, v, v)

    def hue(p, q, t):
        t %= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p
    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    return tuple(round(hue(p, q, h + o) * 255) for o in (1 / 3, 0, -1 / 3))


def _derive_text(panel_rgb):
    """Text tinted with the panel's hue. Starts at a soft extreme (L .16/.97) for
    a premium look and only hardens toward pure black/white as much as needed to
    reach AA 4.5:1 — so mid-tone panels stay legible without looking harsh."""
    h, s, _ = _rgb_hsl(panel_rgb)
    s_text = min(s, 0.18)
    # Pick the direction whose PURE extreme can reach the most contrast.
    go_dark = _contrast(_hsl_rgb((h, s_text, 0.0)), panel_rgb) >= \
        _contrast(_hsl_rgb((h, s_text, 1.0)), panel_rgb)
    L = 0.16 if go_dark else 0.97
    step = -0.01 if go_dark else 0.01
    cand = _hsl_rgb((h, s_text, L))
    guard = 0
    while _contrast(cand, panel_rgb) < 4.5 and 0.0 <= L + step <= 1.0 and guard < 100:
        L += step
        cand = _hsl_rgb((h, s_text, L))
        guard += 1
    return cand


def _derive_alt(panel_rgb, dark):
    """Bubble/input surface: shift LIGHTNESS in HSL (keep hue) so colored panels
    stay clean instead of muddying toward black."""
    h, s, l = _rgb_hsl(panel_rgb)
    s_alt = min(s, 0.5)
    delta = 0.07
    if l > 0.92:
        nl = l - delta
    elif l < 0.10:
        nl = l + delta
    else:
        nl = l + delta if dark else l - delta
    return _hsl_rgb((h, s_alt, max(0.0, min(1.0, nl))))


def _derive_muted(text_rgb, panel_rgb):
    """Muted text from the text color mixed toward the panel, guarded at 3:1."""
    m = _mix(text_rgb, panel_rgb, 0.40)
    if _contrast(m, panel_rgb) < 3.0:
        m = _mix(text_rgb, panel_rgb, 0.25)
    return m


def _border(text_rgb, op):
    return 'rgba(%d,%d,%d,%s)' % (text_rgb[0], text_rgb[1], text_rgb[2], op)


def _widget_theme(client):
    """Widget color tokens from three pickers (accent + background + header).
    Everything else is derived in HSL — text tinted with the panel hue at a soft
    extreme, surfaces shifted by lightness (no muddy mixing), borders neutral —
    so any combination looks polished and stays AA legible.
    """
    accent_rgb = _hex_rgb(getattr(client, 'primary_color', '') or '#34d6c8')
    accent = _hexs(accent_rgb)
    on_accent = _hexs(_on(accent_rgb))

    panel_rgb = _hex_rgb(getattr(client, 'surface_color', '') or '#12304a',
                         fallback=(0x12, 0x30, 0x4a))
    dark = _luminance(panel_rgb) < 0.4
    alt_rgb = _derive_alt(panel_rgb, dark)
    text_rgb = _derive_text(panel_rgb)
    text = _hexs(text_rgb)
    muted = _hexs(_derive_muted(text_rgb, panel_rgb))

    header_rgb = _hex_rgb(getattr(client, 'header_color', '') or _hexs(alt_rgb),
                          fallback=alt_rgb)
    header_text = _hexs(_derive_text(header_rgb))

    return {
        'accent': accent, 'accent_rgb': '%d,%d,%d' % accent_rgb, 'on_accent': on_accent,
        'panel': _hexs(panel_rgb), 'surface_alt': _hexs(alt_rgb),
        'fab_bg': accent, 'fab_text': on_accent,
        'header_bg': _hexs(header_rgb), 'header_text': header_text,
        'text': text, 'muted': muted, 'bubble_a_text': text,
        'border': _border(text_rgb, '0.12'),
        'border_soft': _border(text_rgb, '0.10'),
        'border_bubble': _border(text_rgb, '0.14'),
    }


def widget_js(request):
    """Serve the embeddable chat widget JavaScript for a client (?key=<slug>).

    A client pastes <script src="/widget.js?key=SLUG" async></script> on their
    site; this returns a self-contained widget branded with their config.
    """
    slug = (request.GET.get('key') or 'dominio').strip()[:60] or 'dominio'
    client_obj = Client.objects.filter(slug=slug, is_active=True).first()
    if not client_obj:
        return HttpResponse('/* DOMINIO: unknown or inactive client key */',
                            content_type='application/javascript')
    # Heartbeat for the factory ("is the snippet actually on their site?").
    # Throttled through the cache so a busy site costs one write per 10 min.
    seen_key = f'widgetseen:{client_obj.slug}'
    if not cache.get(seen_key):
        cache.set(seen_key, 1, 600)
        Client.objects.filter(pk=client_obj.pk).update(widget_last_seen_at=timezone.now())
    t = _widget_theme(client_obj)
    ctx = {
        'slug': client_obj.slug,
        # Minted on first serve so tenants created before this existed heal
        # themselves. Handing it out here is safe: reaching this line already
        # required knowing the key, and the widget must present it to chat.
        'widget_token': client_obj.ensure_widget_token(),
        'name': client_obj.name,
        'greeting': client_obj.greeting or agent.DEFAULT_GREETING,
        'color': t['accent'],
        'color_rgb': t['accent_rgb'],
        'on_color': t['on_accent'],
        'panel': t['panel'],
        'surface_alt': t['surface_alt'],
        'text': t['text'],
        'muted': t['muted'],
        'bubble_a_text': t['bubble_a_text'],
        'header_bg': t['header_bg'],
        'header_text': t['header_text'],
        'fab_bg': t['fab_bg'],
        'fab_text': t['fab_text'],
        'border': t['border'],
        'border_soft': t['border_soft'],
        'border_bubble': t['border_bubble'],
        'api_url': request.build_absolute_uri(reverse('chat_api')),
    }
    resp = HttpResponse(render_to_string('landing/widget.js', ctx),
                        content_type='application/javascript')
    resp['Cache-Control'] = 'public, max-age=300'
    return resp


@login_required
@staff_required
def clients_list(request):
    """Factory home: every client agent + its install snippet. Staff only."""
    clients = Client.objects.all()
    for c in clients:
        c.lead_count = c.leads.count()
        c.embed = (f'<script src="{request.build_absolute_uri("/widget.js")}'
                   f'?key={c.slug}" async></script>')
    return render(request, 'landing/dashboard_clients.html', {'clients': clients})


@login_required
@staff_required
@require_POST
def client_toggle_active(request, pk):
    """Pause or reactivate a client agent. Clients are never deleted — pausing
    hides the widget (widget_js requires is_active) while keeping the login,
    leads and config intact, so reactivating later restores everything as-is."""
    client = get_object_or_404(Client, pk=pk)
    client.is_active = not client.is_active
    client.save(update_fields=['is_active'])
    state = 'reactivado' if client.is_active else 'pausado'
    messages.success(request, f'{client.name} {state}.')
    return redirect('clients_list')


@login_required
@staff_required
@require_POST
def client_resend_onboarding(request, pk):
    """Re-send the install email (snippet + a fresh dashboard login) for a client.
    Use when the first email was lost or never sent."""
    client = get_object_or_404(Client, pk=pk)
    embed = (f'<script src="{request.build_absolute_uri("/widget.js")}'
             f'?key={client.slug}" async></script>')
    login = None
    try:
        with transaction.atomic():
            username, temp_password = _provision_client_login(client, reset_password=True)
        if username:
            login = {
                'username': username,
                'password': temp_password,
                'url': request.build_absolute_uri(reverse('dashboard')),
            }
    except Exception:
        logger.exception('Resend provision failed for %s', client.slug)
    sent = _send_onboarding_email(client, embed, login=login)
    Client.objects.filter(pk=client.pk).update(onboarding_sent=True)
    if sent:
        messages.success(request, f'Email de instalación reenviado a {client.notify_email}.')
    else:
        messages.error(
            request,
            f'No se pudo enviar a {client.notify_email} — revisa la configuración de correo.')
    return redirect('clients_list')


@login_required
@staff_required
@require_POST
def client_mark_live(request, pk):
    """Staff flips a self-serve client from 'pending' to 'live' once the
    widget is on their site, and the client gets the 'en vivo' email."""
    client = get_object_or_404(Client, pk=pk)
    if not client.website_url:
        messages.error(request, 'Añade el sitio web del cliente antes de marcarlo en vivo.')
        return redirect('clients_list')
    now = timezone.now()
    had_notes = bool(client.install_notes)
    # The install notes (CMS access the client shared) are only needed until
    # the widget is installed: purge them the moment the agent goes live.
    Client.objects.filter(pk=client.pk).update(
        setup_status='live', live_at=now, is_active=True, install_notes='')
    client.setup_status, client.live_at, client.is_active = 'live', now, True
    client.install_notes = ''
    if had_notes:
        _audit(request, 'install.notes_purged', client=client, target=client.slug)
    sent = _send_live_email(
        client,
        dashboard_url=request.build_absolute_uri(reverse('dashboard')),
        site_url=client.website_url)
    _audit(request, 'client.live', client=client, target=client.slug,
           result='email ok' if sent else 'email failed')
    if sent:
        messages.success(request, f'{client.name} está en vivo — email enviado a {client.notify_email}.')
    else:
        messages.error(
            request,
            f'{client.name} está en vivo, pero no se pudo enviar el email a {client.notify_email}.')
    return redirect('clients_list')


@login_required
@staff_required
def client_form(request, pk=None):
    """Create or edit a client agent (the 'factory' form). Staff only."""
    from .forms import ClientForm

    instance = get_object_or_404(Client, pk=pk) if pk else None
    if request.method == 'POST':
        form = ClientForm(request.POST, instance=instance)
        if form.is_valid():
            try:
                obj = form.save()
            except IntegrityError:
                # Slug is deduped in the form; this only fires on a rare race.
                form.add_error('slug', 'Esa llave se acaba de ocupar — trata de guardar otra vez.')
                return render(request, 'landing/dashboard_client_form.html',
                              {'form': form, 'instance': instance})
            # Auto-write the widget greeting from the agent's knowledge when left
            # blank. Best-effort: any failure just leaves it blank (the widget
            # falls back to DEFAULT_GREETING). Owner can edit/clear to regenerate.
            if not obj.greeting:
                try:
                    greeting = agent.generate_greeting(obj.system_prompt)
                    if greeting:
                        Client.objects.filter(pk=obj.pk).update(greeting=greeting)
                except Exception:
                    logger.exception('Greeting generation failed for %s', obj.slug)
            # Mark-as-Active = payment confirmed → auto-email install instructions,
            # once. (Toggle onboarding_sent off in admin to resend.)
            if obj.is_active and not obj.onboarding_sent:
                embed = (f'<script src="{request.build_absolute_uri("/widget.js")}'
                         f'?key={obj.slug}" async></script>')
                # Auto-provision the client's dashboard login when the agent goes
                # live. Reusing an email that already has a login just adds this
                # agent to it (one login, many agents). Best-effort: a failure
                # must not block activation, and we report what happened.
                login = None
                login_note = ''
                try:
                    with transaction.atomic():
                        username, temp_password = _provision_client_login(obj)
                    if username:
                        login = {
                            'username': username,
                            'password': temp_password,  # None when reusing a login
                            'url': request.build_absolute_uri(reverse('dashboard')),
                        }
                    elif obj.notify_email:
                        # Only happens when the email is a staff account.
                        login_note = (f' (sin acceso al dashboard — {obj.notify_email} es una '
                                      f'cuenta de staff)')
                except Exception:
                    logger.exception('Failed to provision login for client %s', obj.slug)
                    login_note = ' (no se pudo crear el acceso al dashboard)'
                sent = _send_onboarding_email(obj, embed, login=login)
                Client.objects.filter(pk=obj.pk).update(onboarding_sent=True)
                if sent:
                    messages.success(
                        request,
                        f'{obj.name} está activo — email de instalación enviado a '
                        f'{obj.notify_email}{login_note}.')
                else:
                    messages.error(
                        request,
                        f'{obj.name} está activo, pero no se pudo enviar el email de instalación '
                        f'a {obj.notify_email} — revisa la configuración de correo.')
            else:
                messages.success(request, f'Agente de {obj.name} guardado.')
            return redirect('clients_list')
    else:
        form = ClientForm(instance=instance)
    return render(request, 'landing/dashboard_client_form.html',
                  {'form': form, 'instance': instance})
