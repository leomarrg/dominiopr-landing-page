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
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import URLValidator, validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.text import slugify
from django.utils.encoding import force_bytes
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_encode

from landing.tokens import welcome_token
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import agent, entitlements, human, notify, payments
from . import leads as lead_service
from . import usage as usage_ledger
from .emailcheck import check_email
from .forms import ContactForm, LeadNoteForm
from .models import (
    AuditEvent, Booking, Client, ContactSubmission, Conversation,
    LeadNote, Membership, ProcessedWebhookEvent, Subscription, Survey,
    fold_text, match_location, route_contact, rules_for,
)
from .phone import normalize_phone
# Bound of a single visitor message (shared with the channel-agnostic turn
# core so a message is trimmed the same way whichever channel it enters by).
from .turns import MAX_MESSAGE_CHARS, run_turn

logger = logging.getLogger(__name__)
# Hard cap on the raw request body for the chat endpoint (a conversation is tiny).
MAX_CHAT_BODY_BYTES = 64 * 1024


RATE_LIMIT_HTML_MESSAGE = 'Demasiados envíos seguidos. Intenta de nuevo en unos minutos.'


def _rate_limited(prefix, limit, window, methods=None, html=False, anchor=''):
    """Per-IP fixed-window rate limit using the shared cache (no Redis).

    Returns a JSON 429 once `limit` requests from one IP occur within `window`
    seconds. Used to stop bots from burning the Anthropic budget / DoSing the box.

    By default only state-changing requests count (GET/HEAD/OPTIONS page loads
    are free). Pass `methods=('GET', 'POST')` to also throttle reads on views
    whose GET does expensive work (e.g. a Stripe call per request).

    `html=True` is for plain HTML forms: instead of a JSON body the browser
    would show raw, the user is redirected back (safe local Referer, else the
    same path) with a Django messages error. `html='<url name>'` sets the
    fallback target for POST-only views whose own path would 405 on GET.
    """
    counted = tuple(m.upper() for m in methods) if methods else None

    def _html_throttled(request):
        messages.error(request, RATE_LIMIT_HTML_MESSAGE)
        ref = request.META.get('HTTP_REFERER', '')
        if ref and url_has_allowed_host_and_scheme(
                ref, allowed_hosts={request.get_host()},
                require_https=request.is_secure()):
            target = ref
        elif isinstance(html, str):
            target = reverse(html)
        else:
            target = request.path
        if anchor:
            # Browsers drop the #fragment from the Referer: put it back so the
            # page scrolls to the form where the message is shown.
            target = target.split('#')[0] + '#' + anchor
        return redirect(target)

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
                if html:
                    return _html_throttled(request)
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



def _tenant_action_cap_hit(kind, client_obj):
    """Per-tenant, per-day ceiling on the side effects a chat can trigger
    (leads, bookings, escalations to a human).

    The widget token is public by construction (/widget.js hands it to any
    page that knows the slug, and the slug is the company name) and the Origin
    header is whatever a script says it is, so the per-IP caps alone let a
    distributed bot fill one client's CRM, agenda and phone. This bounds that
    to one day's worth per tenant. Returns True when today's cap is already
    reached (the caller answers politely without persisting anything);
    otherwise counts the action and returns False.
    """
    if client_obj is None:
        return False
    cap = int(getattr(settings, 'DOMINIO_TENANT_ACTION_DAILY_CAP', 40))
    key = f'tenantcap:{kind}:{client_obj.slug}:{timezone.localdate():%Y%m%d}'
    used = cache.get(key, 0)
    if used >= cap:
        if used == cap:  # one warning per tenant per day, not one per attempt
            logger.warning('Tenant %s hit the daily %s cap (%s) — likely abuse',
                           client_obj.slug, kind, cap)
            cache.set(key, used + 1, 86400)
        return True
    cache.set(key, used + 1, 86400)
    return False

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
        user = getattr(request, 'user', None)
        AuditEvent.objects.create(
            client=client,
            user=user if getattr(user, 'is_authenticated', False) else None,
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


# Client.clinic_mode: generic text that replaces model-written free text (which
# may carry health details) in stored records and notifications.
CLINIC_ESCALATION_REASON = 'Solicitó hablar con el equipo (modo clínica)'
CLINIC_DASHBOARD_NOTE = 'Revisa el dashboard para los detalles.'


LEAD_CONFIRM_SALT = 'lead-confirm'
LEAD_CONFIRM_MAX_AGE = 7 * 86400


def _lead_email_tag(email):
    """Keyed, non-reversible fingerprint of an email for the confirm token.
    signing.dumps only signs (the payload is plain base64), and the link ends
    up in access logs and mail-scanner logs, so the address itself never goes
    in the URL — only this tag, which still binds the token to that email."""
    return salted_hmac('landing.lead-confirm.email',
                       (email or '').strip().lower()).hexdigest()[:24]


def lead_confirm_url(lead):
    """Absolute double-opt-in link for a lead's email. The token is signed
    (SECRET_KEY + salt), carries the lead id AND a tag of the email it was sent
    to (a later email change voids it), and expires after LEAD_CONFIRM_MAX_AGE
    (checked on use)."""
    token = signing.dumps({'l': lead.pk, 'e': _lead_email_tag(lead.email)},
                          salt=LEAD_CONFIRM_SALT)
    return settings.SITE_URL + reverse('lead_confirm', kwargs={'token': token})


def _send_lead_emails(submission, notify_to=None):
    """Notify the team and ask the visitor to confirm their email.

    `notify_to` lets each client route its leads to its own inbox; defaults to
    DOMINIO's configured address. The team is told right away (a lead is
    worth a call even before the click), with the email marked unconfirmed.
    """
    if notify_to is None:
        notify_to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    business = submission.client.name if submission.client else 'DOMINIO'
    unconfirmed = bool(submission.email and not submission.email_verified_at)
    context = {
        'submission': submission,
        'business': business,
        # Clinic mode: the templates leave the message out and point to the dashboard.
        'clinic_mode': bool(submission.client and submission.client.clinic_mode),
        'clinic_note': CLINIC_DASHBOARD_NOTE,
        # Direct link to the leads dashboard so the client can manage the lead
        # (status, reply) right from the notification email.
        'dashboard_url': settings.SITE_URL + reverse('dashboard'),
        'email_unconfirmed': unconfirmed,
        'confirm_url': lead_confirm_url(submission) if unconfirmed else '',
    }

    # 1) Internal notification to the business inbox.
    if notify_to:
        _send_html_email(
            subject=(f'Nuevo lead para {business}: {submission.name}'
                     + (' (correo sin confirmar)' if unconfirmed else '')),
            to=[notify_to],
            html_template='landing/emails/lead_notification.html',
            txt_template='landing/emails/lead_notification.txt',
            context=context,
            reply_to=[submission.email] if submission.email else None,
        )

    # 2) Confirmation to the person who submitted the form — only if they left an
    # email (phone-only leads can't be emailed; the team follows up by phone).
    # It doubles as the opt-in: a button to confirm the address is theirs.
    if submission.email:
        _send_html_email(
            subject=(f'Confirma tu correo — {business}' if unconfirmed
                     else f'Recibimos tu solicitud — {business}'),
            to=[submission.email],
            html_template='landing/emails/lead_confirmation.html',
            txt_template='landing/emails/lead_confirmation.txt',
            context=context,
            reply_to=[notify_to] if notify_to else None,
        )


def _booking_context(booking, business):
    return {'booking': booking, 'business': business,
            'local_start': timezone.localtime(booking.start),
            'clinic_mode': bool(booking.client and booking.client.clinic_mode),
            'clinic_note': CLINIC_DASHBOARD_NOTE,
            # Phase F: None for single-site businesses (the templates skip it).
            'location': booking.location}


def _send_booking_emails(booking, notify_to=None, business='DOMINIO'):
    """Notify the business of a new booking and confirm it to the visitor."""
    if notify_to is None:
        notify_to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    local = timezone.localtime(booking.start)
    if notify_to:
        _send_html_email(
            subject=f'Nueva cita para {business}: {booking.name} — {local:%d/%m %I:%M %p}',
            to=[notify_to],
            html_template='landing/emails/booking_notification.html',
            txt_template='landing/emails/booking_notification.txt',
            context=_booking_context(booking, business),
            reply_to=[booking.email] if booking.email else None,
        )
    booking_notify_visitor(booking, 'confirmation', business=business,
                           reply_to=[notify_to] if notify_to else None)


# Phase G: the visitor-facing notices a booking can produce, in one place so
# the handler, the dashboard and the reminder cron never diverge on channel.
BOOKING_NOTICE_KINDS = ('confirmation', 'reminder', 'confirmed', 'cancelled')


def booking_notify_visitor(booking, kind, business=None, reply_to=None):
    """Tell the visitor about their booking: `kind` is one of
    BOOKING_NOTICE_KINDS. Email when the booking has one. Otherwise, by the
    channel the booking came from: a WhatsApp booking gets a Meta utility
    template (outside the 24 h window only a template is deliverable) when
    one is configured and the contact has not opted out; failing that, an
    SMS over Twilio when it is configured; failing that, nothing. The
    'confirmation' of a WhatsApp booking is the agent's own reply in the
    same turn, so no separate message goes out. Returns True when the
    notice went out (or there was nothing to send by), False when a send
    was attempted and failed -- the reminder cron retries on False."""
    if kind not in BOOKING_NOTICE_KINDS:
        raise ValueError(f'Unknown booking notice: {kind!r}')
    if business is None:
        business = booking.client.name if booking.client else 'DOMINIO'
    local = timezone.localtime(booking.start)
    where = f' en {booking.location.name}' if booking.location_id else ''
    if booking.email:
        if kind == 'confirmation':
            return _send_html_email(
                subject=f'Recibimos tu cita — {business}',
                to=[booking.email],
                html_template='landing/emails/booking_confirmation.html',
                txt_template='landing/emails/booking_confirmation.txt',
                context=_booking_context(booking, business),
                reply_to=reply_to,
            )
        if kind == 'reminder':
            return _send_plain_email(
                f'Recordatorio de tu cita — {business}', [booking.email],
                (f'¡Hola {booking.name}! Te recordamos tu cita con {business} '
                 f'mañana: {local:%A %d de %B a las %I:%M %p}{where}.\n\n'
                 f'Si necesitas cambiarla, respóndenos este email.\n\n— {business}'))
        if kind == 'confirmed':
            return _send_plain_email(
                f'Tu cita quedó confirmada — {business}', [booking.email],
                f'¡Confirmado! Te esperamos el {local:%A %d de %B a las %I:%M %p}{where}.'
                f'\n\n— {business}')
        return _send_plain_email(
            f'Tu cita fue cancelada — {business}', [booking.email],
            f'Tu cita del {local:%A %d de %B a las %I:%M %p}{where} fue cancelada. '
            f'Si quieres otra fecha, escríbenos o usa el chat.\n\n— {business}')
    if booking.channel == 'whatsapp' and booking.wa_id:
        if kind == 'confirmation':
            return True     # the agent confirms in the same WhatsApp turn
        from . import whatsapp   # late: whatsapp imports this module
        if whatsapp.is_opted_out(booking.client, whatsapp.contact_hash(booking.wa_id)):
            # STOP revokes every notice to that number: no template and no
            # SMS to the same line either. A different phone on the booking
            # (someone else's) still gets the SMS below.
            if not booking.phone or whatsapp.same_number(booking.phone, booking.wa_id):
                return True
        else:
            sent = whatsapp.notify_booking(booking, kind, business)
            if sent is not None:
                return sent
    if booking.phone:
        return notify.send_to(booking.phone, booking_sms_text(booking, business, kind=kind), 'sms')
    return True


def booking_sms_text(booking, business, reminder=False, kind=None):
    """Visitor-facing SMS for a booking (confirmation or 24h reminder). No
    service name: the text is short and, in clinic mode, must carry nothing
    the model wrote. Phase F adds ' en <local>' (accent-free, like the rest
    of the text, so the SMS stays in the GSM-7 alphabet)."""
    local = timezone.localtime(booking.start)
    when = f'{local:%d/%m} a las {local:%I:%M %p}'.replace(' 0', ' ')
    when += booking_sms_where(booking)
    if kind is None:
        kind = 'reminder' if reminder else 'confirmation'
    if kind == 'reminder':
        return (f'{business}: te recordamos tu cita manana, {when}. '
                f'Si necesitas cambiarla, llamanos.')
    if kind == 'confirmed':
        return f'{business}: tu cita del {when} quedo confirmada. Te esperamos.'
    if kind == 'cancelled':
        return (f'{business}: tu cita del {when} fue cancelada. '
                'Si quieres otra fecha, escribenos.')
    return f'{business}: recibimos tu cita para el {when}. Te confirmamos en breve.'


def booking_sms_where(booking):
    """' en Bayamon' for a booking with a location, '' otherwise."""
    if booking.location_id is None:
        return ''
    return f' en {fold_text(booking.location.name).title()}'


def _send_onboarding_email(client, embed_snippet, login=None):
    """Email the client the DIY install instructions (snippet + dashboard login).
    Only for clients set up by hand who install the widget themselves; a
    self-serve buyer gets `_send_welcome_email` (DOMINIO installs for them).

    `login` (optional) is {'username', 'password'|None, 'url'} so the client also
    gets their dashboard credentials — the 'Your own leads dashboard' they're
    paying for. password is None when the account already existed.
    """
    reply_to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    return _send_html_email(
        subject=f'Instala tu agente de IA — instrucciones ({client.name})',
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
    Resend action) rotates the password and mints a fresh one-time link so the
    owner can re-send access even if the first email was lost. Returns (username,
    setup_url); setup_url is None when reusing a login without a reset.
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

    # The account gets a random password nobody ever sees — not even the log —
    # and the customer picks their own through a one-time link. A real (if
    # unknown) password rather than set_unusable_password() so that the
    # "forgot password" form still finds the account if the welcome email is
    # lost: PasswordResetForm skips users without a usable password.
    setup_url = None
    if user is None:
        user = User.objects.create_user(username=email, email=email,
                                        password=secrets.token_urlsafe(32))
        setup_url = _welcome_link(user)
    elif reset_password:
        user.set_password(secrets.token_urlsafe(32))
        user.save(update_fields=['password'])
        setup_url = _welcome_link(user)

    # Link the user to this client (a user may manage many agents). No
    # first-login gate: they never had a password to change.
    Membership.objects.get_or_create(user=user, client=client,
                                     defaults={'must_change_password': False})
    return user.get_username(), setup_url


def _welcome_link(user):
    """Absolute one-time URL where `user` sets their first password."""
    return settings.SITE_URL + reverse('welcome_set_password', kwargs={
        'uidb64': urlsafe_base64_encode(force_bytes(user.pk)),
        'token': welcome_token.make_token(user),
    })


# ============================================================
# MULTI-TENANT ISOLATION — the single place tenant scope is decided
# ============================================================

# Factory + cross-tenant views are DOMINIO staff only.
staff_required = user_passes_test(lambda u: u.is_staff, login_url='login')


def _member_client_ids(user):
    """Client ids this user may operate on in the dashboard (a user can
    manage several agents). Phase E: an 'employee' membership grants only
    the internal assistant page, so it is NOT a dashboard scope — an employee
    of A who is also an admin of B must never see A's leads or transcripts."""
    return list(
        Membership.objects.filter(user=user, role__in=Membership.DASHBOARD_ROLES)
        .values_list('client_id', flat=True))


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


@_rate_limited('contact', limit=5, window=300, html=True, anchor='contact')
def index(request):
    if request.method == 'POST':
        form = ContactForm(request.POST)
        form.human_ip = _client_ip(request)
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


def _chat_lead_handler(request, client_obj=None, page_url='', conversation=None, *,
                       actor_key=None):
    """Build the callback the agent invokes when it captures a lead in chat.

    Saves a ContactSubmission linked to `client_obj` and emails that client's
    own inbox (falling back to DOMINIO's). Returns a short string the model
    feeds back as the tool result. Raises ValueError on a bad email so the
    agent re-asks the visitor. `actor_key` (Phase G) keys the per-visitor
    cap instead of the IP: on a webhook the IP is the provider's.
    """
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    valid_services = {c[0] for c in ContactSubmission.SERVICE_CHOICES}
    notify_to = client_obj.notify_email if client_obj else None
    business_name = client_obj.name if client_obj else 'DOMINIO'
    clinic = bool(client_obj and client_obj.clinic_mode)

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
            # Throwaway inbox or a domain that can't receive mail: nothing is
            # saved, and the model gets the reason in plain words (a raised
            # error only reaches it as a generic "verify the data") so it can
            # ask the visitor for another address.
            ok, reason = check_email(email)
            if not ok:
                return (f'No se guardó el lead: {reason} Pídele al visitante otro '
                        'correo (o un teléfono) y vuelve a intentarlo.')
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
        cap_key = f'leadcap:{actor_key or ip}'
        if cache.get(cap_key, 0) >= 5:
            logger.warning('Chat lead cap reached for IP %s', ip)
            return 'Anotado — el equipo ya tiene tu solicitud y te dará seguimiento por email.'
        if _tenant_action_cap_hit('lead', client_obj):
            return 'Anotado — el equipo ya tiene tu solicitud y te dará seguimiento por email.'

        raw_service = data.get('service')
        service = raw_service if raw_service in valid_services else 'ai-automation'
        if clinic:
            # Nothing the model wrote is stored: even the "service" label can
            # carry health details ("cita por dolor de pecho"), and CLINIC_RULES
            # is a prompt, not a guarantee.
            message = '[Modo clínica] Solicitud desde el chat — el equipo confirma los detalles con la persona.'
        else:
            message = ('[Captured by the AI chat assistant] '
                       + (data.get('summary') or '').strip())[:5000]

        submission = ContactSubmission(
            client=client_obj,
            name=name,
            email=email,
            phone=phone,
            company=(data.get('company') or '').strip()[:160],
            service=service,
            message=message,
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


def _human_handler(request, client_obj=None, conversation=None, page_url='', *,
                   actor_key=None):
    """M-16: the request_human tool. Marks the conversation escalated with the
    reason, saves whatever contact the visitor gave as a lead, and alerts the
    team with the transcript context. Never promises a live chat."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    notify_to = (client_obj.notify_email if client_obj else None) \
        or getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    business_name = client_obj.name if client_obj else 'DOMINIO'
    clinic = bool(client_obj and client_obj.clinic_mode)

    def handle(data):
        if clinic:
            # Never store or forward the model's reason: it may describe symptoms.
            reason = CLINIC_ESCALATION_REASON
        else:
            reason = ((data.get('reason') or '').strip()[:300]
                      or 'El visitante pidió hablar con una persona.')
        name = (data.get('name') or '').strip()[:120]
        email = (data.get('email') or '').strip()[:254]
        phone = normalize_phone((data.get('phone') or '').strip()[:30]) or ''
        if email:
            try:
                validate_email(email)
            except ValidationError:
                email = ''
        # Same as an invalid one: an escalation is never refused over the
        # email, a throwaway/undeliverable address is just not kept.
        if email and not check_email(email)[0]:
            email = ''

        ip = _client_ip(request) or 'unknown'
        cap_key = f'humancap:{actor_key or ip}'
        if cache.get(cap_key, 0) >= 5:
            return 'Anotado — el equipo ya fue notificado y te dará seguimiento.'
        if _tenant_action_cap_hit('human', client_obj):
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
            if not clinic:
                turns = conversation.chat_messages.order_by('position')[:40]
                transcript = '\n'.join(
                    f'{"Visitante" if m.role == "user" else "Agente"}: {m.content}'
                    for m in turns)

        if notify_to:
            body = (f'Una conversación del chat de {business_name} necesita atención humana.\n\n'
                    f'Motivo: {reason}\n'
                    f'Contacto: {name or "—"} {email or phone or "(sin contacto)"}\n'
                    f'Página: {page_url or "—"}\n\n')
            if clinic:
                body += f'Modo clínica: el texto del chat no se incluye. {CLINIC_DASHBOARD_NOTE}'
            else:
                body += f'--- Conversación ---\n{transcript or "(disponible en el dashboard)"}'
            _send_plain_email(
                subject=f'[Atención humana] Chat escalado — {business_name}',
                to=[notify_to], body=body,
                reply_to=[email] if email else None)
        notify.notify_client(
            client_obj,
            f'DOMINIO Chat: un visitante de {business_name} pide atención humana. '
            + (CLINIC_DASHBOARD_NOTE if clinic else f'Motivo: {reason[:120]}'))
        logger.info('Conversation escalated for %s: %s',
                    client_obj.slug if client_obj else 'dominio', reason)
        return ('Escalado: el equipo fue notificado con el contexto y dará seguimiento. '
                'No prometas un tiempo de respuesta específico.')

    return handle


def _booking_handler(request, client_obj=None, *, actor_key=None, channel='web', contact=''):
    """Callback the agent invokes to create a booking. The BACKEND validates the
    time and the DB constraint prevents double-booking — the model only proposes.

    Phase F (locations): with no Location rows the booking is business-wide;
    with exactly one it is assigned silently; with several the tool input
    `location` must name one (match_location) or nothing is booked and the
    model is told to ask. Hours are validated against that location's rules.

    Phase G: `actor_key` keys the per-visitor cap (the IP by default);
    `channel` is stored on the Booking; `contact` is the channel address of
    the visitor (the WhatsApp wa_id) kept for the outbound reminders.
    """
    import datetime as dt
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    from django.db import IntegrityError, transaction

    from .models import Booking

    notify_to = client_obj.notify_email if client_obj else None
    business_name = client_obj.name if client_obj else 'DOMINIO'
    clinic = bool(client_obj and client_obj.clinic_mode)

    def handle(data):
        name = (data.get('name') or '').strip()[:120]
        email = (data.get('email') or '').strip()[:254]
        # Phase D: a 10-digit phone is a valid contact when there is no email.
        phone = normalize_phone((data.get('phone') or '').strip()[:30])
        if not name:
            raise ValueError('Missing name.')
        if not email and not phone:
            if phone is None:
                raise ValueError('Invalid phone number — ask the visitor to re-share it.')
            raise ValueError('Need an email or a 10-digit phone.')
        if email:
            try:
                validate_email(email)
            except ValidationError:
                raise ValueError('Invalid email.')
            # Deliverability only — bookings get no confirmation link. Returned as
            # text (not raised) so the model can tell the visitor what to fix.
            ok, reason = check_email(email)
            if not ok:
                return (f'No se reservó la cita: {reason} Pídele al visitante otro '
                        'correo y vuelve a intentarlo.')
        # A bad phone next to a valid email is simply not kept.
        phone = phone or ''

        # Phase F: which location. Resolved before the caps so a missing
        # location (a soft rejection the model fixes by asking) costs nothing.
        location = None
        locations = list(client_obj.locations.all()) if client_obj is not None else []
        if len(locations) == 1:
            location = locations[0]
        elif len(locations) > 1:
            location = match_location(client_obj, data.get('location'))
            if location is None:
                # Returned as text (not raised): the model never guesses a
                # site; it asks the visitor, naming the real options.
                names = ', '.join(loc.name for loc in locations)
                return ('No se reservó: falta el local. Pregunta en cuál local quiere '
                        f'la cita: {names}.')

        ip = _client_ip(request) or 'unknown'
        cap_key = f'bookcap:{actor_key or ip}'
        if cache.get(cap_key, 0) >= 5:
            return 'Anotado — el equipo te dará seguimiento para confirmar tu hora.'
        if _tenant_action_cap_hit('booking', client_obj):
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
        # Phase F: the location's own rules win over the business-wide ones.
        if client_obj is not None:
            rules = rules_for(client_obj, location)
            if rules:
                local = timezone.localtime(start)
                day_rules = [r for r in rules if r.weekday == local.weekday()]
                ok = any(r.open_time <= local.time() < r.close_time for r in day_rules)
                if not ok:
                    raise ValueError(
                        'That time is outside business hours. Offer a time within '
                        'the business schedule.')

        taken = 'Esa hora ya está ocupada. Ofrécele al visitante otra hora.'
        try:
            with transaction.atomic():
                # Phase F: the DB constraints keep each pool (business-wide,
                # per location) free of duplicates, but not one pool against
                # the other. A slot held by a business-wide booking is busy
                # for every location, and a slot held at any location is busy
                # for a business-wide booking (same reply as the constraint).
                if client_obj is not None:
                    active = Booking.objects.filter(
                        client=client_obj, start=start, status__in=('pending', 'confirmed'))
                    if active.filter(location__isnull=location is not None).exists():
                        return taken
                booking = Booking.objects.create(
                    client=client_obj, name=name, email=email, phone=phone,
                    # Clinic mode: no model-written text, it can carry health details.
                    service='' if clinic else (data.get('service') or '').strip()[:120],
                    # Clinic mode: the model's free-text notes are never stored.
                    notes='' if clinic else (data.get('notes') or '').strip()[:1000],
                    start=start, status='pending', location=location,
                    channel=channel, wa_id=contact if channel == 'whatsapp' else '',
                )
        except IntegrityError:
            return taken

        cache.set(cap_key, cache.get(cap_key, 0) + 1, 3600)
        _send_booking_emails(booking, notify_to=notify_to, business=business_name)
        local = timezone.localtime(start)
        where = f' en {location.name}' if location is not None else ''
        notify.notify_client(
            client_obj,
            f'DOMINIO Chat: nueva cita para {business_name} — {name}, '
            f'{local:%a %d %b, %I:%M %p}{where}.')
        # No visitor PII in the logs — journald outlives our retention policy.
        logger.info('Booking created: booking=%s client=%s at %s',
                    booking.pk, client_obj.slug, local)
        if not email and channel == 'whatsapp':
            # No SMS to a WhatsApp visitor: the reply they are reading IS the
            # confirmation (the model says it in its own words).
            return (f'Reservado para {local:%A %b %d a las %I:%M %p}{where}. '
                    'Confírmale los detalles en tu respuesta; no se envía otro mensaje.')
        via = 'email' if email else 'SMS'
        return (f'Reservado para {local:%A %b %d a las %I:%M %p}{where}. '
                f'Se envió una confirmación por {via}.')

    return handle


def _take_message_handler(request, client_obj=None, conversation=None, page_url='', *,
                          actor_key=None):
    """Phase D: the receptionist's take_message tool. Stores the message as a
    ContactSubmission(source='message'), links it to the conversation, routes
    the alert to the matching StaffContact (person > department > default)
    or to the client's own notify_email/notify_phone, and audits it. The
    backend validates the callback phone; an incomplete one stores nothing
    and tells the model to ask again."""
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email

    business_name = client_obj.name if client_obj else 'DOMINIO'
    clinic = bool(client_obj and client_obj.clinic_mode)
    noted = 'Anotado — el equipo ya tiene tu recado y te dará seguimiento.'

    def handle(data):
        name = (data.get('name') or '').strip()[:120]
        if not name:
            raise ValueError('Missing name.')
        phone = normalize_phone((data.get('callback_phone') or '').strip()[:30])
        if not phone:
            # Returned as text, not raised: the model gets the reason in plain
            # words and re-asks for the one detail (reglas B3).
            return ('No se guardó el recado: el teléfono no es un número completo de '
                    '10 dígitos. Pídele al visitante que lo comparta de nuevo.')
        email = (data.get('email') or '').strip()[:254]
        if email:
            try:
                validate_email(email)
            except ValidationError:
                email = ''
            if email and not check_email(email)[0]:
                email = ''
        department = _single_line(data.get('department'), 80) or 'General'
        recipient = _single_line(data.get('recipient'), 120)
        urgency = data.get('urgency') if data.get('urgency') in ('normal', 'urgent') else 'normal'
        target = recipient or department

        ip = _client_ip(request) or 'unknown'
        cap_key = f'msgcap:{actor_key or ip}'
        if cache.get(cap_key, 0) >= 5:
            logger.warning('Chat message cap reached for IP %s', ip)
            return noted
        if _tenant_action_cap_hit('message', client_obj):
            return noted
        cache.set(cap_key, cache.get(cap_key, 0) + 1, 3600)

        # Phase F: a known Location (its real name, used for routing too);
        # otherwise the model's words stay as free text outside clinic mode.
        loc = match_location(client_obj, data.get('location'))
        if clinic:
            # Nothing the model wrote in free text is stored (B10): the summary
            # may carry health details even if the rules say it must not.
            summary = preferred = ''
            location = loc.name if loc is not None else ''
            message = (f'[Recado] Para: {target} · servicio: no se guarda en modo clínica; '
                       f'el equipo confirma los detalles con la persona.')
            if location:
                message += f' · Local: {location}'
        else:
            summary = _single_line(data.get('summary'), 1000)
            preferred = _single_line(data.get('preferred_time'), 120)
            location = loc.name if loc is not None else _single_line(data.get('location'), 80)
            message = f'[Recado] Para: {target} · Urgencia: {urgency} · {summary}'
            if preferred:
                message += f' · Prefiere llamada: {preferred}'
            if location:
                message += f' · Local: {location}'

        submission = ContactSubmission.objects.create(
            client=client_obj, name=name, email=email, phone=phone,
            service='ai-automation', source='message', message=message[:5000],
            ip_address=ip if ip != 'unknown' else None,
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:300],
            page_url=page_url,
        )
        if conversation is not None:
            Conversation.objects.filter(pk=conversation.pk).update(lead=submission)

        # Routing: a StaffContact wins over the client's default inbox/phone.
        contact = route_contact(client_obj, department=department, recipient=recipient,
                                location=loc)
        notify_to = ((contact.email if contact else '')
                     or (client_obj.notify_email if client_obj else '')
                     or getattr(settings, 'CONTACT_NOTIFY_EMAIL', ''))
        urgent = ' URGENTE' if urgency == 'urgent' else ''
        if notify_to:
            lines = [f'El recepcionista virtual de {business_name} tomó un recado.', '',
                     f'Para: {target}', f'Departamento: {department}',
                     f'De: {name}', f'Teléfono: {phone}']
            if email:
                lines.append(f'Email: {email}')
            lines.append(f'Urgencia: {urgency}')
            if clinic:
                lines.append(f'Modo clínica: el texto del chat no se incluye. {CLINIC_DASHBOARD_NOTE}')
            else:
                lines.append(f'Asunto: {summary or "—"}')
                if preferred:
                    lines.append(f'Prefiere llamada: {preferred}')
            if location:
                lines.append(f'Local: {location}')
            lines += ['', f'Página: {page_url or "—"}',
                      f'Dashboard: {settings.SITE_URL}{reverse("dashboard")}']
            _send_plain_email(
                subject=f'[Recado{urgent}] {target} — {business_name}: {name}',
                to=[notify_to], body='\n'.join(lines),
                reply_to=[email] if email else None)
        sms = (f'DOMINIO Chat: recado{urgent.lower()} para {target} ({business_name}) — '
               f'{name}, {phone}. ' + (CLINIC_DASHBOARD_NOTE if clinic else (summary[:120] or '')))
        if contact is not None:
            notify.notify_client(client_obj, sms, phone=contact.phone, channel=contact.channel)
        else:
            notify.notify_client(client_obj, sms)

        _audit(request, 'lead.message', client=client_obj, target=f'lead:{submission.pk}',
               result=f'{department}|{urgency}')
        logger.info('Message taken via chat: submission=%s client=%s',
                    submission.pk, client_obj.slug if client_obj else '-')
        return (f'Recado guardado para {target}; el equipo de {business_name} le dará '
                'seguimiento. No prometas un tiempo de respuesta.')

    return handle


def _new_conversation_capped(request, client_obj):
    """True when this IP already opened today's allowance of NEW
    conversations on `client_obj` (DOMINIO_NEW_CONVERSATIONS_PER_IP_DAY,
    default 5); otherwise counts this one. Fixed daily window in the shared
    cache, same scheme as the per-visitor message cap."""
    cap = int(getattr(settings, 'DOMINIO_NEW_CONVERSATIONS_PER_IP_DAY', 15) or 0)
    if cap <= 0:
        return False
    today = timezone.now().strftime('%Y%m%d')
    key = f'chat:newconv:{client_obj.slug}:{_client_ip(request) or "unknown"}:{today}'
    opened = cache.get(key, 0)
    if opened >= cap:
        logger.warning('New-conversation cap reached on %s', client_obj.slug)
        return True
    if opened == 0:
        cache.set(key, 1, 60 * 60 * 26)
    else:
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, 60 * 60 * 26)
    return False


def build_handlers(request, client_obj, conversation, page_url='', *, actor_key=None,
                   channel='web', contact=''):
    """The tool callbacks for one turn of `client_obj`'s agent and the rule
    profile to run them under: (handlers, profile). Shared by the web widget
    and the WhatsApp webhook (Phase G) so both channels offer the same tools
    under the same tenant settings. `actor_key` keys the per-visitor caps
    (None = the request IP); `channel`/`contact` are stamped on bookings."""
    common = {'actor_key': actor_key}
    if client_obj is not None and client_obj.agent_profile == 'receptionist':
        # Phase D: the front desk takes messages instead of capturing leads or
        # escalating (take_message and request_human overlap; the model would
        # pick at random). Bookings stay opt-in.
        handlers = {'take_message': _take_message_handler(
            request, client_obj, conversation=conversation, page_url=page_url, **common)}
        profile = 'receptionist'
    else:
        handlers = {'capture_lead': _chat_lead_handler(
            request, client_obj, page_url=page_url, conversation=conversation, **common)}
        if client_obj is not None:
            handlers['request_human'] = _human_handler(
                request, client_obj, conversation=conversation, page_url=page_url, **common)
        profile = 'web'
    if client_obj and client_obj.enable_bookings:
        handlers['create_booking'] = _booking_handler(
            request, client_obj, channel=channel, contact=contact, **common)
    return handlers, profile


def _global_daily_cap_hit():
    """Global daily ceiling across ALL IPs and channels -- the defense per-IP
    limits can't give: caps worst-case daily API spend even under a
    distributed/botnet attack. True when today's budget is spent; otherwise
    counts this turn. Shared by the web widget and the WhatsApp webhook."""
    daily_cap = getattr(settings, 'DOMINIO_AGENT_DAILY_CAP', 2000)
    day_key = 'chat:global:' + timezone.now().strftime('%Y%m%d')
    used = cache.get(day_key, 0)
    if used >= daily_cap:
        logger.warning('Daily chat cap (%s) reached', daily_cap)
        return True
    if used == 0:
        cache.set(day_key, 1, 60 * 60 * 26)  # ~26h TTL covers the whole day
    else:
        try:
            cache.incr(day_key)
        except ValueError:
            cache.set(day_key, 1, 60 * 60 * 26)
    return False


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

    # Phase 5: a solved hCaptcha (the signed "human pass", bound to this slug
    # and the widget's visitor id) BEFORE any counter below moves, so a bot
    # never consumes a tenant's daily quota. After the body checks above, so a
    # malformed request (400) does not spend the pass's message budget. Read
    # from the JSON body, not a header: the CORS preflight only allows
    # Content-Type.
    if human.enabled() and not human.check_pass(
            payload.get('human'), client_obj.slug if client_obj else 'dominio',
            payload.get('visitor')):
        return human.challenge_response()

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

    # Global daily ceiling across ALL IPs (shared with the WhatsApp webhook).
    if _global_daily_cap_hit():
        return JsonResponse(
            {'error': 'Nuestro asistente está descansando por ahora. Usa el formulario de '
                      'contacto y el equipo te responde enseguida.'},
            status=503,
        )

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
            conversation = Conversation.objects.filter(
                client=client_obj, widget_session=session).first()
            if conversation is None:
                # A conversation is the tenant's BILLABLE unit (Phase C). The
                # session id is minted by the caller, so without this one IP
                # could open hundreds of "conversations" a day and inflate a
                # client's month. Existing sessions never hit this check.
                if _new_conversation_capped(request, client_obj):
                    resp = JsonResponse(
                        {'error': 'Demasiadas solicitudes. Baja un poco la velocidad.'},
                        status=429)
                    resp['Retry-After'] = str(60 * 60)
                    return resp
                conversation, _created = Conversation.objects.get_or_create(
                    client=client_obj, widget_session=session,
                    defaults={'page_url': page,
                              'config_version': client_obj.config_version()})

    # The caller controls the request body, so the 'assistant' turns it sends
    # are not evidence of anything we said: run_turn rebuilds the history from
    # the server-owned transcript and takes ONLY the new user turn from here.
    handlers, profile = build_handlers(request, client_obj, conversation, page_url=page)
    turn_kwargs = {'profile': profile} if profile != 'web' else {}

    # From here on the turn is channel-agnostic (landing.turns): history,
    # model call, persistence and the usage ledger. This view only maps the
    # agent's failures to the widget's HTTP contract.
    try:
        # Without a stored conversation the sanitized body history is the
        # only context there is (old widgets, unknown slug); with one it is
        # ignored and the server-owned transcript wins.
        result = run_turn(client_obj, conversation, history[-1]['content'],
                          channel='web', handlers=handlers, page_url=page,
                          fallback_history=history[:-1], **turn_kwargs)
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
    return JsonResponse({'reply': result.reply})


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

    # Phase 5: human pass before the demo's caps move (see chat_api).
    if human.enabled() and not human.check_pass(
            payload.get('human'), human.DEMO_KEY, payload.get('visitor')):
        return human.challenge_response()

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
        reply, usage = agent.answer(history, business_prompt=context, handlers={})
    except agent.AgentNotConfigured:
        return JsonResponse({'error': 'El demo no está disponible ahora mismo.'}, status=503)
    except anthropic.RateLimitError:
        return JsonResponse({'error': 'Ocupado — trata de nuevo en un momento.'}, status=429)
    except anthropic.APIError:
        logger.exception('Anthropic API error in demo_api')
        return JsonResponse({'error': 'Algo salió mal. Trata de nuevo.'}, status=502)

    usage_ledger.record('demo', usage)
    return JsonResponse({'reply': reply})


@xframe_options_exempt
@csrf_exempt
@_rate_limited('humanverify', limit=20, window=600)
def human_verify(request):
    """Phase 5: the hCaptcha checkbox the chat widgets open in an iframe
    (?key=<slug>&visitor=<id>&origin=<parent origin>).

    Framed cross-site on purpose, so it opts out of X-Frame-Options and sends
    its own CSP whose frame-ancestors lists only the origins allowed for that
    key. csrf_exempt because inside a cross-site iframe the CSRF cookie is a
    blocked third-party cookie; the hCaptcha token itself is the protection.
    A solved checkbox posts a signed pass to the parent window — targeted at
    the exact validated origin, never '*'.
    """
    if not human.enabled():
        raise Http404
    if request.method not in ('GET', 'POST'):
        return HttpResponse(status=405)
    src = request.POST if request.method == 'POST' else request.GET
    key = (src.get('key') or 'dominio').strip()[:60] or 'dominio'
    visitor = human.clean_visitor(src.get('visitor'))
    origin = human.normalize_origin(src.get('origin'))

    if key == human.DEMO_KEY:
        client_obj = None
    else:
        client_obj = Client.objects.filter(slug=key, is_active=True).first()
        if client_obj is None and key != 'dominio':
            return HttpResponse('Agente desconocido.', status=404,
                                content_type='text/plain; charset=utf-8')
    if not visitor or not origin:
        return HttpResponse('Solicitud inválida.', status=400,
                            content_type='text/plain; charset=utf-8')
    ancestors = human.frame_ancestors(key, client_obj, origin, request)
    if ancestors is None:
        return HttpResponse('Este dominio no está permitido.', status=403,
                            content_type='text/plain; charset=utf-8')

    nonce = secrets.token_urlsafe(16)
    ctx = {'key': key, 'visitor': visitor, 'origin': origin, 'nonce': nonce,
           'error': '', 'pass_data': None}
    if request.method == 'POST':
        if human.verify(request.POST.get('h-captcha-response'), _client_ip(request)):
            ctx['pass_data'] = {'type': 'dominio-human-pass',
                                'pass': human.issue_pass(key, visitor),
                                'visitor': visitor}
        else:
            ctx['error'] = human.FORM_ERROR
    resp = render(request, 'landing/human_verify.html', ctx)
    resp['Content-Security-Policy'] = human.challenge_csp(ancestors, nonce)
    resp['Cache-Control'] = 'no-store'
    return resp


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
    {'id': 'starter', 'name': 'Starter', 'monthly': 149, 'annual': 1490, 'setup': 500,
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
     'features': ['Todo lo de Pro', 'Soporte prioritario', 'Hasta 1,500 mensajes al día',
                  'WhatsApp y multicanal (próximamente)',
                  'Varios locales o marcas (próximamente)', 'Conexión con tu CRM y calendario (a la medida)']},
    {'id': 'custom', 'name': 'Custom', 'monthly': None, 'annual': None, 'setup': None,
     'tagline': 'Integraciones y flujos a la medida',
     'features': ['Todo lo de Scale', 'Locales y canales ilimitados',
                  'Integraciones y flujos a la medida', 'Onboarding dedicado']},
]


# Phase C: the packages (docs/precios/Propuesta-paquetes-DOMINIO.pdf). Sold
# direct, so `public: False` keeps them off /empezar/ until they are published.
# Each has three price steps (Entrada / Medio / Tope) selected by
# Subscription.step; `monthly`/`annual`/`setup` at the top level mirror step 1
# so every consumer of a PLANS entry keeps working (use plan_pricing() to get
# the step the client actually pays). Annual = 10x monthly. Capacity is
# measured in CONVERSATIONS per calendar month (America/Puerto_Rico).
def _package(plan_id, name, tagline, setup, steps, features):
    return {
        'id': plan_id, 'name': name, 'public': False, 'tagline': tagline, 'setup': setup,
        'monthly': steps[0]['monthly'], 'annual': steps[0]['annual'],
        'steps': steps, 'features': features,
    }


def _step(monthly, conversations, wa_conversations=0, seats=0):
    return {'monthly': monthly, 'annual': round(monthly * 10, 2),
            'monthly_conversations': conversations,
            'wa_conversations': wa_conversations, 'seats': seats}


PACKAGES = [
    _package('small', 'Chat Básico', 'Contesta preguntas y no pierde clientes fuera de horario', 199,
             [_step(49.99, 150), _step(74.99, 300), _step(99.99, 450)],
             ['Agente de IA en tu página web', 'Respuestas 24/7 y captura de leads',
              'Dashboard privado', 'Instalación Express en 48 horas']),
    _package('medium', 'Recepcionista', 'Atiende, agenda y toma recados', 499,
             [_step(149, 500), _step(219, 1000), _step(299, 1500)],
             ['Todo lo de Chat Básico', 'Citas y reservaciones automáticas',
              'Recados y escalación a tu equipo', 'Afinamos tu agente cada mes']),
    _package('large', 'Recepcionista + Equipo', 'Recepcionista para clientes y asistente para tu equipo', 999,
             [_step(349, 1500, seats=5), _step(449, 3000, seats=10), _step(599, 4000, seats=15)],
             ['Todo lo de Recepcionista', 'Asistente interno para empleados (con login)',
              'Hasta 3 locales con su horario', 'Avisos por departamento o local']),
    _package('premium', 'Premium WhatsApp', 'Tus clientes te escriben por WhatsApp', 1499,
             [_step(699, 4000, 500, 15), _step(899, 4000, 1000, 15), _step(1199, 4000, 2000, 25)],
             ['Todo lo de Recepcionista + Equipo', 'WhatsApp Business con número propio',
              'Confirmaciones y recordatorios por WhatsApp', 'Hasta 25 asientos internos']),
]
PLANS.extend(PACKAGES)
PLAN_BY_ID = {p['id']: p for p in PLANS}
# What /empezar/ shows and accepts: the launch plans + Custom.
PUBLIC_PLANS = [p for p in PLANS if p.get('public', True)]

# Price of each overage block, in cents, charged at month end by
# `manage.py bill_overage` (never by cutting the service). Only 'web_block'
# applies to the current channels; the WhatsApp and seat hooks are ready.
OVERAGE_PRICES = {
    'web_block': (100, 1500),    # +100 web conversations: $15
    'wa_block': (500, 14900),    # +500 WhatsApp conversations: $149
    'seat': (1, 900),            # extra internal seat: $9/month
}
# Human copy of the same numbers (dashboard, emails, terms, pricing FAQ).
OVERAGE_WEB_BLOCK, OVERAGE_WEB_PRICE = OVERAGE_PRICES['web_block'][0], '$15'
# Messages the abuse ceiling allows per included conversation per day: the
# daily cap of a package is 3x its average daily conversations, in messages.
_MESSAGES_PER_CONVERSATION = 6


def _package_daily_cap(step):
    convs = step['monthly_conversations'] + step['wa_conversations']
    return max(60, -(-convs * 3 * _MESSAGES_PER_CONVERSATION // 30))


def _package_limits(plan_id, enable_bookings, channels, locations):
    plan = PLAN_BY_ID[plan_id]
    steps = []
    for s in plan['steps']:
        steps.append({'monthly_conversations': s['monthly_conversations'],
                      'wa_conversations': s['wa_conversations'], 'seats': s['seats'],
                      'daily_message_cap': _package_daily_cap(s)})
    base = {'unit': 'conversations', 'enable_bookings': enable_bookings,
            'channels': set(channels), 'locations': locations,
            'overage': dict(OVERAGE_PRICES), 'steps': steps}
    base.update(steps[0])   # step 1 at the top level, for the provisioning code
    return base


# What each paid tier unlocks on the tenant at provisioning time (webhook).
# Kept next to PLANS so pricing copy and enforcement never drift apart.
# unit='messages': the launch plans, measured by the daily cap only.
# unit='conversations': the packages; `steps[i]` holds the capacity of step
# i+1 and plan_limits(plan, step) resolves it. Client.entitlement_overrides
# may override any of these keys per client (landing.entitlements).
PLAN_LIMITS = {
    # Phase F: 'locations' = how many Location rows a tenant may create
    # (landing.entitlements; the dashboard refuses more, never deletes extras).
    'starter': {'daily_message_cap': 200, 'enable_bookings': False, 'unit': 'messages',
                'locations': 1},
    'pro': {'daily_message_cap': 500, 'enable_bookings': True, 'unit': 'messages',
            'locations': 1},
    'scale': {'daily_message_cap': 1500, 'enable_bookings': True, 'unit': 'messages',
              'locations': 3},
    'custom': {'daily_message_cap': 3000, 'enable_bookings': True, 'unit': 'messages',
               'locations': 50},
    'small': _package_limits('small', False, ('web',), 1),
    'medium': _package_limits('medium', True, ('web',), 1),
    'large': _package_limits('large', True, ('web', 'internal'), 3),
    'premium': _package_limits('premium', True, ('web', 'internal', 'whatsapp'), 3),
}


def plan_limits(plan, step=1):
    """The limits of `plan` at price `step` (1-based; clamped). Unknown plan
    -> an empty 'messages' entry (no monthly capacity). Returns a fresh dict."""
    base = PLAN_LIMITS.get(plan)
    if base is None:
        return {'unit': 'messages', 'enable_bookings': False, 'daily_message_cap': 0}
    out = {k: (set(v) if isinstance(v, set) else dict(v) if isinstance(v, dict) else v)
           for k, v in base.items() if k != 'steps'}
    steps = base.get('steps') or []
    if steps:
        idx = min(max(int(step or 1), 1), len(steps)) - 1
        out.update(steps[idx])
        out['step'] = idx + 1
    return out


def plan_pricing(plan, step=1):
    """A PLANS entry with monthly/annual resolved to `step` (packages) --
    what the client actually pays. None for an unknown plan."""
    entry = PLAN_BY_ID.get(plan)
    if entry is None:
        return None
    out = {k: v for k, v in entry.items() if k != 'steps'}
    steps = entry.get('steps') or []
    if steps:
        s = steps[min(max(int(step or 1), 1), len(steps)) - 1]
        out.update({'monthly': s['monthly'], 'annual': s['annual'], 'step': steps.index(s) + 1})
    return out


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
    plan = plan_pricing(sub.plan, sub.step)
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
            username, setup_url = _provision_client_login(existing)
        client, created = existing, False
    else:
        # Everything in system_prompt is effectively PUBLIC: the agent repeats
        # it to any visitor who asks ("what's the owner's cell?"). The owner's
        # contact and what they told us at checkout are for DOMINIO's install
        # crew, so they go to install_notes (staff-only), never to the prompt.
        prompt_lines = [f'Negocio: {company}']
        if website:
            prompt_lines.append(f'Sitio web: {website}')
        prompt_lines.append('')
        notes_lines = []
        if contact_name or contact_phone:
            notes_lines.append(
                f'Contacto: {contact_name or "—"}' + (f' ({contact_phone})' if contact_phone else ''))
        if lead is not None and lead.message.strip():
            notes_lines.append(f'Lo que el cliente nos contó al registrarse:\n{lead.message.strip()}')
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
                            install_notes='\n'.join(notes_lines),
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
            username, setup_url = _provision_client_login(client)
        created = True

        # Best-effort greeting from the seed knowledge (same as the factory form).
        greeting_usage = agent.new_usage()
        try:
            greeting = agent.generate_greeting(client.system_prompt, usage=greeting_usage)
            if greeting:
                Client.objects.filter(pk=client.pk).update(greeting=greeting)
        except Exception:
            logger.exception('Greeting generation failed for %s', client.slug)
        usage_ledger.record('greeting', greeting_usage, client=client)

    dashboard_url = settings.SITE_URL + reverse('dashboard')
    login = ({'username': username, 'setup_url': setup_url, 'url': dashboard_url}
             if username else None)
    embed = _embed_snippet(settings.SITE_URL, client.slug)
    install_url = settings.SITE_URL + reverse('install')

    # The activation link exists in exactly one place: this email. If it does not
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
        # Phase 5: first, so a bot never reaches the email DNS lookups below.
        if human.enabled() and not human.verify(
                request.POST.get('h-captcha-response'), _client_ip(request)):
            return render(request, 'landing/get_started.html',
                          {'plans': PUBLIC_PLANS, 'form': request.POST,
                           'errors': {'human': human.FORM_ERROR}})
        if not name:
            errors['name'] = 'Requerido.'
        if not company:
            errors['company'] = 'Requerido.'
        if plan not in {p['id'] for p in PUBLIC_PLANS}:
            errors['plan'] = 'Escoge un plan para continuar.'
        # Email OR phone — a phone-only signup is fine, but a given phone must be valid.
        if email:
            try:
                validate_email(email)
            except ValidationError:
                errors['email'] = 'Escribe un email válido.'
            else:
                ok, reason = check_email(email)
                if not ok:
                    errors['email'] = reason
        phone = normalize_phone(phone)
        if phone is None:
            errors['phone'] = 'Escribe un número de teléfono válido, ej. (787) 123-4567.'
            phone = ''
        if not email and not phone:
            errors['email'] = 'Déjanos un email o un teléfono para poder contactarte.'
        if errors:
            return render(request, 'landing/get_started.html',
                          {'plans': PUBLIC_PLANS, 'form': request.POST, 'errors': errors})

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
                  {'plans': PUBLIC_PLANS, 'form': {}, 'errors': {}})


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
                        # .update() skips auto_now: bill_overage uses
                        # updated_at to send a mid-month plan change to
                        # manual review instead of charging it blindly.
                        updates['updated_at'] = timezone.now()
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


def _lead_from_confirm_token(token):
    """(lead, error_state). error_state is 'expired' or 'invalid' when the
    token is too old, tampered with, points at a deleted lead, or was issued
    for an email the lead no longer has."""
    try:
        data = signing.loads(token, salt=LEAD_CONFIRM_SALT, max_age=LEAD_CONFIRM_MAX_AGE)
        lead_id, token_tag = int(data["l"]), str(data["e"])
    except signing.SignatureExpired:
        return None, 'expired'
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None, 'invalid'
    lead = ContactSubmission.objects.select_related('client').filter(pk=lead_id).first()
    if lead is None or not lead.email or not hmac.compare_digest(
            _lead_email_tag(lead.email), token_tag):
        return None, 'invalid'
    return lead, ''


@_rate_limited('leadconfirm', limit=20, window=300, methods=('GET', 'POST'))
def lead_confirm(request, token):
    """Double opt-in for a lead's email (link in the confirmation email).

    GET only shows a "Confirmar mi correo" button and never writes: Outlook
    Safe Links and other scanners open every link in an email, which must not
    count as the person confirming. The POST (CSRF-protected) does the work,
    idempotently. Bad, expired or mismatched tokens get a friendly 400.
    """
    if request.method not in ('GET', 'POST'):
        return HttpResponse(status=405, headers={'Allow': 'GET, POST'})
    lead, error = _lead_from_confirm_token(token)
    if error:
        resp = render(request, 'landing/lead_confirm.html', {'state': error}, status=400)
    else:
        business = lead.client.name if lead.client else 'DOMINIO'
        if request.method == 'POST':
            # Conditional UPDATE: a double click or a replayed POST never moves
            # the first confirmation timestamp.
            updated = ContactSubmission.objects.filter(
                pk=lead.pk, email_verified_at__isnull=True,
            ).update(email_verified_at=timezone.now())
            if updated:
                logger.info('Lead email confirmed: submission=%s', lead.pk)
            state = 'done'
        else:
            state = 'already' if lead.email_verified_at else 'ask'
        resp = render(request, 'landing/lead_confirm.html',
                      {'state': state, 'business': business, 'token': token})
    # The token is in the URL: keep it out of shared caches and Referer headers.
    resp['Cache-Control'] = 'no-store'
    resp['Referrer-Policy'] = 'no-referrer'
    return resp


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
        _audit(request, 'lead.status', client=lead.client,
               target=f'lead:{lead.pk}', result=new_status)
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
    # Never the address: the audit trail is visible to every org_admin.
    _audit(request, 'lead.email', client=lead.client,
           target=f'lead:{lead.pk}', result='sent' if sent else 'failed')
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


# ============================================================
# PHASE 7 — lead detail: timeline, notes, delete / anonymize
# ============================================================

def _scoped_lead(user, pk):
    """get_lead_or_404 with the client joined (the detail page needs it)."""
    return get_object_or_404(leads_for(user).select_related('client'), pk=pk)


def _can_manage_lead(user, lead):
    """Delete/anonymize: staff, or an org_admin of the lead's own client.
    A lead with no client (DOMINIO's own site) is staff-only — the membership
    lookup with client=None matches nothing."""
    if user.is_staff:
        return True
    return bool(lead.client_id) and _is_org_admin(user, lead.client)


@login_required
def lead_detail(request, pk):
    """One lead with everything around it, in a single timeline. Any member
    of the lead's client may read it and add notes; only an org_admin may
    delete or anonymize."""
    lead = _scoped_lead(request.user, pk)
    clinic = bool(lead.client and lead.client.clinic_mode)
    # Reading a lead (and its transcript excerpts) is access to PII: audit it
    # once per user+lead per hour, like conversation_detail.
    try:
        first_view = cache.add(f'auditview:lead:{request.user.pk}:{lead.pk}', 1, 3600)
    except Exception:  # best-effort: a cache hiccup must not 500 the page
        first_view = True
    if first_view:
        _audit(request, 'lead.view', client=lead.client, target=f'lead:{lead.pk}')
    return render(request, 'landing/dashboard_lead_detail.html', {
        'active_tab': 'leads',
        'lead': lead,
        'clinic': clinic,
        'timeline': lead_service.build_timeline(lead, clinic=clinic),
        'status_choices': ContactSubmission.STATUS_CHOICES,
        'note_form': LeadNoteForm(),
        'note_max': LeadNote.MAX_CHARS,
        'can_manage': _can_manage_lead(request.user, lead),
        'default_from': settings.DEFAULT_FROM_EMAIL,
    })


@login_required
@require_POST
def lead_note(request, pk):
    """Add an internal note. Any member of the lead's client (agent included)."""
    lead = _scoped_lead(request.user, pk)
    form = LeadNoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, form.errors['text'][0])
        return redirect('lead_detail', pk=lead.pk)
    LeadNote.objects.create(lead=lead, author=request.user, text=form.cleaned_data['text'])
    # pk only: the note text may carry personal details.
    _audit(request, 'lead.note', client=lead.client, target=f'lead:{lead.pk}')
    messages.success(request, 'Nota guardada.')
    return redirect('lead_detail', pk=lead.pk)


def _lead_privacy_action(request, pk, action):
    """Shared flow for delete/anonymize: scope (404) -> role (403) -> a
    confirmation page on the first POST -> act on the second POST with
    confirm=1. Audited with the lead pk and row counts only."""
    lead = _scoped_lead(request.user, pk)
    if not _can_manage_lead(request.user, lead):
        return HttpResponseForbidden(
            'Solo un administrador de la organización puede borrar o anonimizar leads.')
    if request.POST.get('confirm') != '1':
        return render(request, 'landing/dashboard_lead_confirm.html', {
            'active_tab': 'leads', 'lead': lead, 'action': action,
            'counts': lead_service.related_counts(lead),
        })
    client, lead_pk = lead.client, lead.pk
    if action == 'delete':
        counts = lead_service.delete_lead(lead)
    else:
        counts = lead_service.anonymize_lead(lead)
    _audit(request, f'lead.{action}', client=client, target=f'lead:{lead_pk}',
           result=(f'conv={counts["conversations"]} msgs={counts["messages"]} '
                   f'citas={counts["bookings"]} notas={counts["notes"]}'))
    if action == 'delete':
        messages.success(
            request,
            f'Lead eliminado, con {counts["conversations"]} conversación(es) y '
            f'{counts["bookings"]} cita(s).')
    else:
        messages.success(
            request,
            'Lead anonimizado: se borraron sus datos personales, notas y el texto '
            'de sus conversaciones. Las estadísticas se conservan.')
    return redirect('dashboard')


@login_required
@require_POST
def lead_delete(request, pk):
    return _lead_privacy_action(request, pk, 'delete')


@login_required
@require_POST
def lead_anonymize(request, pk):
    return _lead_privacy_action(request, pk, 'anonymize')


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


class WelcomeSetPasswordView(DashboardPasswordResetConfirmView):
    """The link in the welcome email: same form and template as a reset, but
    validated with the 7-day welcome token instead of the 1-hour reset one.
    Single-use falls out of the token embedding the password hash."""
    token_generator = welcome_token


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
    plan = plan_pricing(subscription.plan, subscription.step) if subscription else None
    return render(request, 'landing/dashboard_billing.html', {
        'active_tab': 'billing', 'active': 'billing',
        'client': client, 'subscription': subscription, 'plan': plan,
        'allowance': entitlements.allowance_status(client),
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
    # The token is minted on first serve so tenants created before it existed
    # heal themselves. It is NOT a secret (the slug is the company name), it
    # only raises the bar for the casual copy of a snippet onto another site:
    # when the tenant has an allowlist and the browser tells us which page is
    # loading the script, a page outside that list gets the widget without a
    # token, so its chats are refused. No Referer (privacy policies, scripts)
    # still gets the token — the daily per-tenant caps are the real bound.
    referer_host = (urlparse(request.META.get('HTTP_REFERER', '')).hostname or '').lower()
    token_ok = not (client_obj.allowed_origins and referer_host
                    and referer_host not in set(client_obj.origin_list()))
    ctx = {
        'slug': client_obj.slug,
        'widget_token': client_obj.ensure_widget_token() if token_ok else '',
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
        # Phase 5: absolute, from the public domain (never the Host header):
        # the iframe origin the widget trusts for the human pass.
        'challenge_url': settings.SITE_URL.rstrip('/') + reverse('human_verify'),
        'clinic_mode': client_obj.clinic_mode,
    }
    resp = HttpResponse(render_to_string('landing/widget.js', ctx),
                        content_type='application/javascript')
    resp['Cache-Control'] = 'public, max-age=300'
    return resp


@login_required
@staff_required
def clients_list(request):
    """Factory home: every client agent + its install snippet. Staff only."""
    clients = Client.objects.annotate(lead_count=Count('leads'))
    month_usage = usage_ledger.rollup_by_client(*usage_ledger.month_bounds())
    widget_src = request.build_absolute_uri('/widget.js')
    for c in clients:
        used = month_usage.get(c.pk, {})
        c.month_messages = used.get('messages', 0)
        c.month_cost = used.get('cost', Decimal(0)).quantize(Decimal('0.0001'))
        c.embed = f'<script src="{widget_src}?key={c.slug}" async></script>'
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
    """Re-send the client's first email with a fresh dashboard activation link.
    Use when the first email was lost or never sent.

    Which email depends on the flow the client is in: a self-serve buyer still
    `pending` was promised "we install it for you" in the welcome email, so
    that is what gets re-sent — never the DIY install email, which would hand
    them back the work they paid for and claim the agent is live."""
    client = get_object_or_404(Client, pk=pk)
    embed = (f'<script src="{request.build_absolute_uri("/widget.js")}'
             f'?key={client.slug}" async></script>')
    login = None
    try:
        with transaction.atomic():
            username, setup_url = _provision_client_login(client, reset_password=True)
        if username:
            login = {
                'username': username,
                'setup_url': setup_url,
                'url': request.build_absolute_uri(reverse('dashboard')),
            }
    except Exception:
        logger.exception('Resend provision failed for %s', client.slug)
    if client.setup_status == 'pending':
        sub = getattr(client, 'subscription', None)
        sent = _send_welcome_email(
            client, _plan_name(sub.plan if sub else ''), login,
            request.build_absolute_uri(reverse('install')), embed,
            setup_fee_charged=bool(sub and sub.setup_fee_charged))
        label = 'de bienvenida'
    else:
        sent = _send_onboarding_email(client, embed, login=login)
        label = 'de instalación'
    Client.objects.filter(pk=client.pk).update(onboarding_sent=True)
    if sent:
        messages.success(request, f'Email {label} reenviado a {client.notify_email}.')
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
                greeting_usage = agent.new_usage()
                try:
                    greeting = agent.generate_greeting(obj.system_prompt, usage=greeting_usage)
                    if greeting:
                        Client.objects.filter(pk=obj.pk).update(greeting=greeting)
                except Exception:
                    logger.exception('Greeting generation failed for %s', obj.slug)
                usage_ledger.record('greeting', greeting_usage, client=obj)
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
                        username, setup_url = _provision_client_login(obj)
                    if username:
                        login = {
                            'username': username,
                            'setup_url': setup_url,  # None when reusing a login
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
