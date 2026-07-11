import json
import logging
import secrets
from functools import wraps
from urllib.parse import urlparse

import anthropic
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import PasswordChangeForm
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import agent
from .forms import ContactForm
from .models import Client, ContactSubmission, Membership
from .phone import normalize_phone

logger = logging.getLogger(__name__)

# Bound a single visitor message so the public endpoint can't be abused with
# huge payloads.
MAX_MESSAGE_CHARS = 2000
# Hard cap on the raw request body for the chat endpoint (a conversation is tiny).
MAX_CHAT_BODY_BYTES = 64 * 1024


def _rate_limited(prefix, limit, window):
    """Per-IP fixed-window rate limit using the shared cache (no Redis).

    Returns a JSON 429 once `limit` requests from one IP occur within `window`
    seconds. Used to stop bots from burning the Anthropic budget / DoSing the box.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            # Only throttle state-changing requests; GET/HEAD page loads are free.
            if request.method in ('GET', 'HEAD', 'OPTIONS'):
                return view(request, *args, **kwargs)
            ip = _client_ip(request) or 'unknown'
            key = f'rl:{prefix}:{ip}'
            count = cache.get(key, 0)
            if count >= limit:
                resp = JsonResponse(
                    {'error': 'Too many requests. Please slow down.'}, status=429)
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
    """Real client IP, honoring the Nginx X-Forwarded-For header."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _page_url(raw):
    """Normalize a visitor-page URL (widget location.href or Referer header):
    only http(s), trimmed to the model's 500-char cap. Empty string otherwise."""
    if not isinstance(raw, str):
        return ''
    raw = raw.strip()
    if not raw.lower().startswith(('http://', 'https://')):
        return ''
    return raw[:500]


def _send_html_email(subject, to, html_template, txt_template, context, reply_to=None):
    """Send a multipart (text + HTML) email. Logs and swallows errors.

    Returns True on success, False on failure — automatic callers can ignore it,
    but manual flows (e.g. replying to a lead) use it to show real feedback.
    """
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
                'Thank you. We received your message and will get back to you soon.',
            )
            return redirect(reverse('index') + '#contact')
        messages.error(request, 'Please review the highlighted fields and try again.')
    else:
        form = ContactForm()
    return render(request, 'landing/index.html', {'form': form})


def _chat_lead_handler(request, client_obj=None, page_url=''):
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
            return 'Noted — the team already has your request and will follow up by email.'

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
        _send_lead_emails(submission, notify_to=notify_to)
        cache.set(cap_key, cache.get(cap_key, 0) + 1, 3600)  # 1-hour window
        logger.info('Lead captured via chat: %s <%s>', name, email)
        return f'Lead saved and the {business_name} team has been notified by email.'

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
            return 'Noted — the team will follow up to confirm your time.'

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

        try:
            with transaction.atomic():
                booking = Booking.objects.create(
                    client=client_obj, name=name, email=email,
                    service=(data.get('service') or '').strip()[:120],
                    notes=(data.get('notes') or '').strip()[:1000],
                    start=start, status='pending',
                )
        except IntegrityError:
            return 'That time slot is already taken. Offer the visitor a different time.'

        cache.set(cap_key, cache.get(cap_key, 0) + 1, 3600)
        _send_booking_emails(booking, notify_to=notify_to, business=business_name)
        local = timezone.localtime(start)
        logger.info('Booking created: %s <%s> at %s', name, email, local)
        return f'Booked for {local:%A %b %d at %I:%M %p}. A confirmation was emailed.'

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
        return JsonResponse({'error': 'Method not allowed.'}, status=405)

    # Reject oversized bodies before parsing them into memory.
    if len(request.body) > MAX_CHAT_BODY_BYTES:
        return JsonResponse({'error': 'Request too large.'}, status=413)

    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)

    # Resolve which business this agent represents (multi-tenant). The widget
    # sends its public "client" slug; DOMINIO's own site defaults to 'dominio'.
    slug = payload.get('client')
    slug = slug.strip()[:60] if isinstance(slug, str) else 'dominio'
    client_obj = Client.objects.filter(slug=slug or 'dominio', is_active=True).first()
    business_prompt = client_obj.system_prompt if client_obj else None

    # Origin allowlist: the widget may only run on the client's own domains.
    origin = request.META.get('HTTP_ORIGIN', '')
    if client_obj and client_obj.allowed_origins and origin:
        allowed = set(client_obj.origin_list())
        o = urlparse(origin)
        if o.netloc.lower() not in allowed and (o.hostname or '').lower() not in allowed:
            return JsonResponse({'error': 'This domain is not allowed.'}, status=403)

    raw = payload.get('messages')
    if not isinstance(raw, list) or not raw:
        return JsonResponse({'error': 'No messages provided.'}, status=400)

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
        return JsonResponse({'error': 'No question to answer.'}, status=400)

    # Global daily ceiling across ALL IPs — the defense per-IP limits can't give:
    # caps worst-case daily API spend even under a distributed/botnet attack.
    daily_cap = getattr(settings, 'DOMINIO_AGENT_DAILY_CAP', 2000)
    day_key = 'chat:global:' + timezone.now().strftime('%Y%m%d')
    used = cache.get(day_key, 0)
    if used >= daily_cap:
        logger.warning('Daily chat cap (%s) reached', daily_cap)
        return JsonResponse(
            {'error': 'Our assistant is resting for now. Please use the contact form '
                      'and the team will get right back to you.'},
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

    handlers = {'capture_lead': _chat_lead_handler(request, client_obj, page_url=page)}
    if client_obj and client_obj.enable_bookings:
        handlers['create_booking'] = _booking_handler(request, client_obj)

    try:
        reply = agent.answer(history, business_prompt=business_prompt, handlers=handlers)
    except agent.AgentNotConfigured:
        return JsonResponse(
            {'error': 'The assistant is not available right now.'}, status=503
        )
    except anthropic.RateLimitError:
        return JsonResponse(
            {'error': 'We are getting a lot of questions right now — please try again in a moment.'},
            status=429,
        )
    except anthropic.APIError:
        logger.exception('Anthropic API error in chat_api')
        return JsonResponse(
            {'error': 'Something went wrong reaching the assistant. Please try again, or use the contact form.'},
            status=502,
        )

    return JsonResponse({'reply': reply})


@csrf_exempt
@_rate_limited('demo', limit=15, window=60)
def demo_api(request):
    """Public 'try your own agent' preview. Caller pastes their business context;
    the agent answers using it (ephemeral, nothing saved, no lead capture). Shares
    the same daily budget cap and rate limits as the live chat to bound cost."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed.'}, status=405)
    if len(request.body) > MAX_CHAT_BODY_BYTES * 2:
        return JsonResponse({'error': 'Too much text. Trim it down a bit.'}, status=413)
    try:
        payload = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'Invalid request.'}, status=400)

    context = (payload.get('context') or '').strip()[:6000]
    if not context:
        return JsonResponse({'error': 'Add a few lines about your business first.'}, status=400)

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
        return JsonResponse({'error': 'No question to answer.'}, status=400)

    # Shared daily ceiling (same budget guard as the live chat).
    daily_cap = getattr(settings, 'DOMINIO_AGENT_DAILY_CAP', 2000)
    day_key = 'chat:global:' + timezone.now().strftime('%Y%m%d')
    used = cache.get(day_key, 0)
    if used >= daily_cap:
        return JsonResponse({'error': 'The demo is busy right now. Try again later.'}, status=503)
    if used == 0:
        cache.set(day_key, 1, 60 * 60 * 26)
    else:
        try:
            cache.incr(day_key)
        except ValueError:
            cache.set(day_key, 1, 60 * 60 * 26)

    try:
        reply = agent.answer(history, business_prompt=context, handlers={})
    except agent.AgentNotConfigured:
        return JsonResponse({'error': 'The demo is not available right now.'}, status=503)
    except anthropic.RateLimitError:
        return JsonResponse({'error': 'Busy — please try again in a moment.'}, status=429)
    except anthropic.APIError:
        logger.exception('Anthropic API error in demo_api')
        return JsonResponse({'error': 'Something went wrong. Please try again.'}, status=502)

    return JsonResponse({'reply': reply})


# ============================================================
# GET STARTED — public pricing + agent signup funnel (lean self-serve)
# ============================================================

PLANS = [
    {'id': 'starter', 'name': 'Starter', 'price': '$99',
     'annual': '$990/yr — 2 months free',
     'features': ['AI agent on your website', 'Answers customer questions 24/7',
                  'Turns visitors into leads — straight to your inbox',
                  'Private dashboard to see every lead']},
    {'id': 'pro', 'name': 'Pro', 'price': '$249', 'featured': True,
     'annual': '$2,490/yr — 2 months free',
     'features': ['Everything in Starter', 'Qualifies leads before they reach you',
                  'Books appointments & reservations automatically',
                  'Reply to leads right from your dashboard',
                  'Matched to your brand & colors',
                  'We fine-tune your agent every month']},
    {'id': 'scale', 'name': 'Scale', 'price': '$499',
     'annual': '$4,990/yr — 2 months free',
     'features': ['Everything in Pro', 'WhatsApp & multi-channel',
                  'Up to 3 locations or brands', 'Connects to your CRM & calendar',
                  'Priority support']},
    {'id': 'custom', 'name': 'Custom', 'price': "Let's talk", 'setup': 'Quote',
     'features': ['Everything in Scale', 'Unlimited locations & channels',
                  'Custom integrations & workflows', 'Dedicated onboarding']},
]


@_rate_limited('signup', limit=5, window=300)
def get_started(request):
    """Public pricing page + 'get your AI agent' request form."""
    plan_names = {p['id']: p['name'] for p in PLANS}
    if request.method == 'POST':
        if request.POST.get('hp'):  # honeypot
            return redirect('get_started')
        company = (request.POST.get('company') or '').strip()[:160]
        name = (request.POST.get('name') or '').strip()[:120]
        email = (request.POST.get('email') or '').strip()[:254]
        phone = (request.POST.get('phone') or '').strip()[:30]
        website = (request.POST.get('website_url') or '').strip()[:200]
        plan = (request.POST.get('plan') or '').strip()[:40]
        details = (request.POST.get('message') or '').strip()[:3000]

        errors = {}
        if not name:
            errors['name'] = 'Required.'
        if not company:
            errors['company'] = 'Required.'
        # Email OR phone — a phone-only signup is fine, but a given phone must be valid.
        if email:
            try:
                validate_email(email)
            except ValidationError:
                errors['email'] = 'Enter a valid email.'
        phone = normalize_phone(phone)
        if phone is None:
            errors['phone'] = 'Enter a valid phone number, e.g. (787) 123-4567.'
            phone = ''
        if not email and not phone:
            errors['email'] = 'Leave an email or a phone so we can reach you.'
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
        messages.success(
            request,
            'Got it! We will email you shortly to set up your agent and payment.')
        return redirect(reverse('get_started') + '#done')

    return render(request, 'landing/get_started.html',
                  {'plans': PLANS, 'form': {}, 'errors': {}})


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
    """
    # First sign-in on an auto-provisioned account: force the temp password out
    # of circulation before showing any data.
    if Membership.objects.filter(user=request.user, must_change_password=True).exists():
        messages.info(request, 'Please set a new password to finish setting up your account.')
        return redirect('password_change')

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
        messages.error(request, 'This lead has no email — reach them by phone instead.')
        return redirect(ref)

    if not subject or not body:
        messages.error(request, 'Subject and message are both required.')
        return redirect(ref)

    # Per-user daily cap: every reply goes out through DOMINIO's single shared
    # Gmail (reputation + ~500/day quota). One tenant must not be able to burn it.
    cap_key = f'mailcap:{request.user.pk}'
    if cache.get(cap_key, 0) >= 50:
        messages.error(request, 'Daily email limit reached. Please try again tomorrow.')
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
        messages.success(request, f'Email sent to {lead.email}.')
    else:
        messages.error(
            request,
            f'Could not send the email to {lead.email}. Check the mail settings and try again.')
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
            messages.success(request, 'Your password has been updated.')
            return redirect('dashboard')
    else:
        form = PasswordChangeForm(request.user)
    return render(request, 'landing/dashboard_password.html', {'form': form})


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
    t = _widget_theme(client_obj)
    ctx = {
        'slug': client_obj.slug,
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
    state = 'reactivated' if client.is_active else 'paused'
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
        messages.success(request, f'Install email resent to {client.notify_email}.')
    else:
        messages.error(
            request,
            f'Could not send to {client.notify_email} — check the mail settings.')
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
                form.add_error('slug', 'That key was just taken — try saving again.')
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
                        login_note = (f' (no dashboard login — {obj.notify_email} is a '
                                      f'staff account)')
                except Exception:
                    logger.exception('Failed to provision login for client %s', obj.slug)
                    login_note = ' (dashboard login could not be created)'
                sent = _send_onboarding_email(obj, embed, login=login)
                Client.objects.filter(pk=obj.pk).update(onboarding_sent=True)
                if sent:
                    messages.success(
                        request,
                        f'{obj.name} is live — install email sent to '
                        f'{obj.notify_email}{login_note}.')
                else:
                    messages.error(
                        request,
                        f'{obj.name} is live, but the install email to '
                        f'{obj.notify_email} could not be sent — check the mail settings.')
            else:
                messages.success(request, f'Agent for {obj.name} saved.')
            return redirect('clients_list')
    else:
        form = ClientForm(instance=instance)
    return render(request, 'landing/dashboard_client_form.html',
                  {'form': form, 'instance': instance})
