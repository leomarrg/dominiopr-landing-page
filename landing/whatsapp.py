"""Phase G -- the visitors' WhatsApp chat, straight through Meta's Cloud API.

No SDK, no BSP, no Twilio: Meta POSTs every inbound message to
/api/whatsapp/webhook/ (signed with the app secret), this module resolves the
tenant by the number's `phone_number_id` (ChannelAccount), runs the same
channel-agnostic turn as the web widget (landing.turns.run_turn with the
tenant's tools from views.build_handlers) and sends the reply back with
`POST /{version}/{phone_number_id}/messages` over urllib, inside the same
request, before answering 200. There is no in-line reply (nothing like
TwiML), no thread and no queue: Meta retries an event that gets no 200
within seconds, so the turn is bounded (timeout=10, max_retries=1) and the
idempotency key on the message id (`wamid`) absorbs a retry that overlaps a
slow turn.

What a tenant's number costs us: nothing inside the 24 h customer-service
window that every inbound message opens (the agent's replies and the
booking confirmation ride on it). Business-initiated notices outside it
(reminders, status changes from the dashboard) must be approved UTILITY
templates (send_template); without one configured, or for a contact that
said STOP (ChannelOptOut), views.booking_notify_visitor falls back to the
Twilio SMS it already had, or sends nothing.

Privacy: the visitor's number is never stored on a Conversation; the row is
keyed by a salted HMAC of it (contact_hash) and a day. `contacts[].profile
.name` never reaches the prompt. Bookings keep the number (Booking.wa_id)
because the reminder has to be sent somewhere, under the same retention as
Booking.phone.
"""
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.error
import urllib.request

import anthropic
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import agent, entitlements, views
from .models import AuditEvent, ChannelAccount, ChannelOptOut, Conversation
from .turns import run_turn

logger = logging.getLogger(__name__)

# A webhook body is a few KB; nginx caps the path at 256k as well.
MAX_BODY_BYTES = 256 * 1024
# Seconds per Graph API call, and per model call inside the turn (the SDK is
# set to one retry). Meta's own patience is "a few seconds"; past that it
# retries the event, which the wamid key absorbs.
GRAPH_TIMEOUT = 10
TURN_TIMEOUT = 10
# Meta's limit for a text body.
MAX_TEXT_CHARS = 4096
# A conversation = one window of inactivity (and never across a month: the
# billable unit is "conversations started in the month").
SESSION_WINDOW = dt.timedelta(hours=24)
# A message older than this is a redelivery after an outage: recorded in the
# log, never answered (the visitor has long moved on).
STALE_AFTER = dt.timedelta(hours=24)
HASH_SALT = 'landing.whatsapp.contact'
# Audit 'whatsapp.send_failed' at most this often per tenant.
SEND_FAILED_AUDIT_TTL = 3600

# Whole-message keywords (lowercase, outer punctuation stripped). Never a
# substring match, and never "cancelar": a visitor who answers a reminder
# with it wants the appointment cancelled, not to be unsubscribed.
STOP_WORDS = frozenset({'stop', 'baja', 'unsubscribe', 'no más', 'no mas'})
START_WORDS = frozenset({'start', 'alta', 'unstop'})

# Message types we never download: the visitor gets a fixed notice (or the
# caption is answered when there is one).
MEDIA_TYPES = ('image', 'audio', 'video', 'document', 'sticker', 'location', 'contacts')

# Fixed texts, never written by the model, by Client.primary_language.
TEXTS = {
    'es': {
        'media': ('No puedo ver imágenes, audios ni archivos por aquí. Escríbeme tu '
                  'pregunta en texto y te ayudo.'),
        'stop': ('Listo, no te enviaremos más mensajes por WhatsApp. Escribe START si '
                 'quieres volver a recibirlos.'),
        'start': 'Listo, volverás a recibir nuestros mensajes por WhatsApp.',
        'cap': ('Llegaste al límite de mensajes por hoy. Llámanos o escríbenos mañana y '
                'seguimos con gusto.'),
        'busy': 'El asistente llegó a su límite de hoy. Llámanos o escríbenos mañana.',
        'clinic': ('Este número no atiende consultas por WhatsApp. Llámanos y con gusto te '
                   'ayudamos.'),
        'error': 'Disculpa, no pude responder ahora mismo. Escríbeme de nuevo en un momento.',
    },
    'en': {
        'media': ("I can't see images, audio or files here. Type your question and I'll "
                  'help you.'),
        'stop': ("Done, we won't send you more WhatsApp messages. Reply START to receive "
                 'them again.'),
        'start': 'Done, you will receive our WhatsApp messages again.',
        'cap': ("You've reached today's message limit. Call us or write again tomorrow."),
        'busy': "The assistant reached its limit for today. Call us or write again tomorrow.",
        'clinic': "This number doesn't take enquiries over WhatsApp. Please call us.",
        'error': "Sorry, I couldn't answer right now. Please write again in a moment.",
    },
}


def _t(client, key):
    lang = getattr(client, 'primary_language', 'es') if client is not None else 'es'
    return TEXTS.get(lang, TEXTS['es'])[key]


def _day(now=None):
    """'YYYYMMDD' of `now` in Puerto Rico: the day the caps and session ids
    are keyed by, independent of settings.TIME_ZONE."""
    return timezone.localtime(now or timezone.now(), entitlements.BUSINESS_TZ).strftime('%Y%m%d')


def _once(key, ttl=86400):
    """True the first time `key` is seen within `ttl` (cache.add); True as
    well when the cache is down, so a notice is never silently lost."""
    try:
        return cache.add(key, 1, ttl)
    except Exception:
        return True


def _counter_hit(key, limit, window=60 * 60 * 26):
    """Fixed-window counter on the shared cache: True once `limit` hits
    happened inside `window` seconds (the hit is counted otherwise). Same
    scheme as the widget's daily counters (26 h TTL covers the whole day)."""
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


# ------------------------------------------------------------------
# Identity and signature
# ------------------------------------------------------------------

def contact_hash(wa_id):
    """Stable, non-reversible id of a WhatsApp contact (the `wa_id`, E.164
    digits without '+'). Keyed by DOMINIO_PHONE_HASH_KEY so a SECRET_KEY
    rotation does not split every contact from their history and opt-out.
    32 hex chars: enough to never collide, short enough for external_id."""
    key = getattr(settings, 'DOMINIO_PHONE_HASH_KEY', '') or settings.SECRET_KEY
    return salted_hmac(HASH_SALT, str(wa_id), secret=key, algorithm='sha256').hexdigest()[:32]


def sign(raw_body, secret=None):
    """The X-Hub-Signature-256 value Meta would send for `raw_body` (bytes).
    Used by wa_simulate and the tests."""
    secret = secret if secret is not None else getattr(settings, 'META_APP_SECRET', '')
    return 'sha256=' + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


_HEX64 = re.compile(r'[0-9a-fA-F]{64}')


def verify_signature(raw_body, header):
    """True when `header` is HMAC-SHA256(META_APP_SECRET, raw body). The RAW
    bytes: Meta escapes non-ASCII in its JSON, so re-serializing the parsed
    payload would never match. The header must be 'sha256=' + 64 hex chars;
    anything else is False without touching compare_digest (which raises
    TypeError on non-ASCII str, and an unauthenticated 500 would email the
    admins once per request)."""
    secret = getattr(settings, 'META_APP_SECRET', '')
    if not secret or not header or not header.startswith('sha256='):
        return False
    given = header[len('sha256='):].strip()
    if not _HEX64.fullmatch(given):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), given.lower().encode())


def same_number(phone, wa_id):
    """True when `phone` (any format) is the same line as `wa_id` (E.164
    digits): their last 10 digits match. Used to honour a STOP given over
    WhatsApp for SMS to that same number."""
    digits = re.sub(r'\D', '', str(phone or ''))
    tail = re.sub(r'\D', '', str(wa_id or ''))[-10:]
    return bool(digits) and bool(tail) and digits.endswith(tail)


# ------------------------------------------------------------------
# Graph API (outbound)
# ------------------------------------------------------------------

class GraphError(Exception):
    """A failed Graph API call: HTTP status (0 = network), Meta's error code
    (e.g. 190 token, 131047 window closed, 131026 not on WhatsApp, 130429
    rate limit) and its message. Never carries the recipient."""

    def __init__(self, status, code, message=''):
        super().__init__(f'graph {status} code={code} {message}'.strip())
        self.status, self.code, self.message = status, code, message


def _graph_url(path):
    version = getattr(settings, 'META_GRAPH_VERSION', 'v23.0') or 'v23.0'
    return f'https://graph.facebook.com/{version}/{path.lstrip("/")}'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx from graph.facebook.com is an error, never something to follow:
    following it would hand the bearer token to whatever host the redirect
    names."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _urlopen(req, timeout=GRAPH_TIMEOUT):
    """The one place the Graph API is reached (tests patch it)."""
    return _opener.open(req, timeout=timeout)


def _error_of(body):
    try:
        err = json.loads(body or b'{}').get('error') or {}
        return err.get('code', ''), str(err.get('message', ''))[:200]
    except (ValueError, AttributeError):
        return '', ''


def _graph_post(path, payload, retry=True):
    """POST JSON to the Graph API with the System User token. Returns the
    decoded response. One retry on 5xx or a network error when `retry`
    (never on 4xx: those are our payload, the token or the recipient).
    Raises GraphError. The single seam the tests and wa_simulate mock."""
    token = getattr(settings, 'META_WA_ACCESS_TOKEN', '')
    if not token:
        raise GraphError(0, 'no_token', 'META_WA_ACCESS_TOKEN is not set')
    data = json.dumps(payload).encode()
    attempts = 2 if retry else 1
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            _graph_url(path), data=data, method='POST',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with _urlopen(req, timeout=GRAPH_TIMEOUT) as resp:
                try:
                    return json.loads(resp.read() or b'{}')
                except ValueError:
                    return {}
        except urllib.error.HTTPError as exc:
            # 3xx arrives here too (redirects are not followed): no retry.
            code, message = _error_of(exc.read())
            if exc.code >= 500 and attempt < attempts:
                continue
            raise GraphError(exc.code, code, message) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < attempts:
                continue
            raise GraphError(0, 'network', exc.__class__.__name__) from None
    raise GraphError(0, 'network', 'unreachable')   # pragma: no cover


def graph_get(path):
    """GET a Graph API node (preflight: `GET /{phone_number_id}`). Raises
    GraphError."""
    token = getattr(settings, 'META_WA_ACCESS_TOKEN', '')
    if not token:
        raise GraphError(0, 'no_token', 'META_WA_ACCESS_TOKEN is not set')
    req = urllib.request.Request(_graph_url(path), headers={'Authorization': f'Bearer {token}'})
    try:
        with _urlopen(req, timeout=GRAPH_TIMEOUT) as resp:
            return json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as exc:
        code, message = _error_of(exc.read())
        raise GraphError(exc.code, code, message) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise GraphError(0, 'network', exc.__class__.__name__) from None


def _audit_send_failed(client, exc):
    """One 'whatsapp.send_failed' AuditEvent per tenant per hour: a dead
    token (190/401) or a closed window shows up in the audit without a row
    per message. Best-effort."""
    if client is None or not _once(f'wa:sendfail:{client.slug}', SEND_FAILED_AUDIT_TTL):
        return
    try:
        AuditEvent.objects.create(client=client, action='whatsapp.send_failed',
                                  target=f'http:{exc.status}',
                                  result=f'{exc.code} {exc.message}'[:200])
    except Exception:
        logger.exception('Audit write failed for whatsapp.send_failed')


def _send(account, payload, kind):
    try:
        _graph_post(f'{account.phone_number_id}/messages', payload)
        return True
    except GraphError as exc:
        # The code, never the recipient: journald outlives the retention policy.
        logger.warning('WhatsApp %s send failed client=%s http=%s code=%s',
                       kind, account.client.slug, exc.status, exc.code)
        _audit_send_failed(account.client, exc)
        return False


def split_text(text, limit=MAX_TEXT_CHARS):
    """`text` in pieces of at most `limit` chars, cut at a line break or a
    space when one exists in the second half of the piece. [] for blank."""
    text = (text or '').strip()
    if not text:
        return []
    parts = []
    while len(text) > limit:
        cut = text.rfind('\n', 0, limit)
        if cut < limit // 2:
            cut = text.rfind(' ', 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def send_text(account, wa_id, text):
    """Free-form text to `wa_id` (inside the 24 h window). Longer than Meta's
    4,096 chars is sent as consecutive messages. True when every piece went."""
    for chunk in split_text(text):
        payload = {'messaging_product': 'whatsapp', 'recipient_type': 'individual',
                   'to': wa_id, 'type': 'text',
                   'text': {'preview_url': False, 'body': chunk}}
        if not _send(account, payload, 'text'):
            return False
    return True


def send_template(account, wa_id, name, params, language='es'):
    """An approved template (business-initiated, outside the window) with
    its body parameters in order. Meta rejects empty parameters and line
    breaks or tabs inside them: callers pass a real value for every slot
    and each one is collapsed to a single line here."""
    payload = {
        'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': wa_id,
        'type': 'template',
        'template': {'name': name, 'language': {'code': language},
                     'components': [{'type': 'body',
                                     'parameters': [{'type': 'text',
                                                     'text': views._single_line(p, 1024)}
                                                    for p in params]}]},
    }
    return _send(account, payload, 'template')


def mark_read(account, wamid):
    """Blue ticks on the visitor's message before the model runs. Best-effort
    and without retry: it must never cost the turn its time budget."""
    try:
        _graph_post(f'{account.phone_number_id}/messages',
                    {'messaging_product': 'whatsapp', 'status': 'read', 'message_id': wamid},
                    retry=False)
    except GraphError as exc:
        logger.debug('WhatsApp mark-as-read failed client=%s code=%s', account.client.slug, exc.code)


# ------------------------------------------------------------------
# Outbound booking notices (views.booking_notify_visitor)
# ------------------------------------------------------------------

def account_for(client):
    if client is None:
        return None
    return ChannelAccount.objects.filter(client=client, provider='meta', channel='whatsapp',
                                         is_active=True).first()


def is_opted_out(client, chash):
    return ChannelOptOut.objects.filter(client=client, channel='whatsapp',
                                        contact_hash=chash).exists()


def booking_template(booking, kind, business):
    """(template name, body params) for a business-initiated booking notice,
    or None when no template is configured for `kind`. Params match the
    templates proposed in DEPLOYMENT.md: business, date/time, place (the
    location, or the business itself for single-site tenants: a slot can
    never be empty), and for status changes the new state."""
    local = timezone.localtime(booking.start)
    when = f'{local:%d/%m} a las {local:%I:%M %p}'.replace(' 0', ' ')
    where = booking.location.name if booking.location_id else business
    if kind == 'reminder':
        name = getattr(settings, 'META_WA_TEMPLATE_REMINDER', '')
        params = [business, when, where]
    elif kind in ('confirmed', 'cancelled'):
        name = getattr(settings, 'META_WA_TEMPLATE_STATUS', '')
        params = [business, when, where, 'confirmada' if kind == 'confirmed' else 'cancelada']
    else:
        return None
    return (name, params) if name else None


def notify_booking(booking, kind, business):
    """Send the `kind` notice of a WhatsApp booking as a template. None when
    WhatsApp cannot carry it (no token, no active number, no template for
    this kind, or the contact opted out) so the caller falls back to SMS;
    otherwise the send result."""
    if not getattr(settings, 'META_WA_ACCESS_TOKEN', '') or not booking.wa_id:
        return None
    account = account_for(booking.client)
    if account is None:
        return None
    template = booking_template(booking, kind, business)
    if template is None:
        return None
    if is_opted_out(booking.client, contact_hash(booking.wa_id)):
        return None
    return send_template(account, booking.wa_id, template[0], template[1])


# ------------------------------------------------------------------
# Inbound: session, parsing
# ------------------------------------------------------------------

def contact_label(wa_id):
    """How the inbox names a WhatsApp contact that has no lead yet: the last
    four digits only ("WhatsApp · termina en 1234"), never the number. '' when
    the id is too short to be a phone number."""
    digits = ''.join(ch for ch in str(wa_id or '') if ch.isdigit())
    if len(digits) < 4:
        return ''
    return f'WhatsApp · termina en {digits[-4:]}'


def resolve_conversation(client, chash, now=None, wa_id=''):
    """The Conversation this message belongs to: the contact's latest one
    when it is still inside SESSION_WINDOW of inactivity AND started in the
    current Puerto Rico month; otherwise a new row keyed
    'wa:{hash}:{YYYYMMDD}' (today's). So one conversation = one 24 h
    window, never crossing a month, never one per contact for life.
    `wa_id` (when given) only feeds contact_label on a NEW row."""
    now = now or timezone.now()
    prefix = f'wa:{chash}:'
    last = (Conversation.objects
            .filter(client=client, channel='whatsapp', external_id__startswith=prefix)
            .order_by('-last_message_at').first())
    month_start, _end = entitlements.month_bounds(now)
    if (last is not None and last.last_message_at >= now - SESSION_WINDOW
            and last.started_at >= month_start):
        return last
    conversation, _created = Conversation.objects.get_or_create(
        client=client, channel='whatsapp', external_id=f'{prefix}{_day(now)}',
        defaults={'config_version': client.config_version(),
                  'contact_label': contact_label(wa_id)})
    return conversation


def extract_text(message):
    """(text, kind) of an inbound message. kind 'text': answer `text`
    (text body, button text, interactive reply title, or a media caption);
    'media': an image/audio/file with no caption, fixed notice; 'ignore':
    reactions, unsupported and anything else. Media is never downloaded."""
    mtype = message.get('type')
    if mtype == 'text':
        return ((message.get('text') or {}).get('body') or '').strip(), 'text'
    if mtype == 'button':
        return ((message.get('button') or {}).get('text') or '').strip(), 'text'
    if mtype == 'interactive':
        inter = message.get('interactive') or {}
        reply = inter.get('button_reply') or inter.get('list_reply') or {}
        return (reply.get('title') or '').strip(), 'text'
    if mtype in MEDIA_TYPES:
        caption = ((message.get(mtype) or {}).get('caption') or '').strip()
        return (caption, 'text') if caption else ('', 'media')
    return '', 'ignore'


def _keyword(text):
    return text.lower().strip(' .!¡¿?,')


def _stale(message):
    try:
        ts = int(message.get('timestamp') or 0)
    except (TypeError, ValueError):
        return False
    if not ts:
        return False
    sent_at = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return timezone.now() - sent_at > STALE_AFTER


# ------------------------------------------------------------------
# Inbound: the webhook
# ------------------------------------------------------------------

def _verify(request):
    """Meta's subscription handshake: echo hub.challenge when hub.verify_token
    is ours. An empty configured token never matches."""
    expected = getattr(settings, 'META_WA_VERIFY_TOKEN', '')
    token = request.GET.get('hub.verify_token', '')
    challenge = request.GET.get('hub.challenge', '')[:200]
    # Bytes on both sides: compare_digest raises on a non-ASCII str, and an
    # unauthenticated 500 here would email the admins per request.
    if (request.GET.get('hub.mode') == 'subscribe' and expected and challenge
            and hmac.compare_digest(token.encode('utf-8', 'surrogateescape'),
                                    expected.encode())):
        return HttpResponse(challenge, content_type='text/plain')
    return HttpResponseForbidden()


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def whatsapp_webhook(request):
    """GET: verification. POST: events from Meta. After the signature check
    the answer is ALWAYS 200: a non-200 makes Meta retry the batch and,
    repeated, disable the webhook. Each message is handled on its own (one
    failing never blocks the rest of the batch). Status updates (sent,
    delivered, read) are acknowledged and dropped."""
    if request.method == 'GET':
        return _verify(request)
    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({'error': 'too large'}, status=413)
    if not getattr(settings, 'META_APP_SECRET', ''):
        # Once per hour, not per request: anyone can hit this URL.
        if _once('wa:nosecret', 3600):
            logger.warning('WhatsApp webhook called but META_APP_SECRET is not set')
        return HttpResponse(status=503)
    if not verify_signature(request.body, request.META.get('HTTP_X_HUB_SIGNATURE_256', '')):
        logger.warning('WhatsApp webhook: bad signature from %s', views._client_ip(request))
        return HttpResponseForbidden()
    try:
        return _dispatch(request)
    except Exception as exc:
        # Never a 500 to the outside: each one emails the admins, and Meta
        # would retry the batch. The class only (no body, no PII).
        logger.warning('WhatsApp webhook: unhandled %s', exc.__class__.__name__)
        return HttpResponse(status=200)


def _dispatch(request):
    """The signed POST: parse, resolve the tenant per change, handle each
    message on its own."""
    try:
        payload = json.loads(request.body)
    except ValueError:
        return JsonResponse({'error': 'bad json'}, status=400)
    if not isinstance(payload, dict) or payload.get('object') != 'whatsapp_business_account':
        return HttpResponse('ignored')

    for entry in payload.get('entry') or []:
        for change in (entry.get('changes') or []) if isinstance(entry, dict) else []:
            value = change.get('value') if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            if value.get('statuses'):
                logger.debug('WhatsApp status update(s): %d', len(value['statuses']))
            messages = value.get('messages') or []
            if not messages:
                continue
            phone_number_id = str((value.get('metadata') or {}).get('phone_number_id') or '')
            account = (ChannelAccount.objects
                       .filter(provider='meta', channel='whatsapp',
                               phone_number_id=phone_number_id, is_active=True,
                               client__is_active=True)
                       .select_related('client').first())
            for message in messages:
                if not isinstance(message, dict):
                    continue
                try:
                    _handle_message(request, account, phone_number_id, message)
                except Exception:
                    # WARNING with the trace, not ERROR: the 'landing' logger
                    # mails the admins at ERROR and this runs once per message.
                    logger.warning('WhatsApp message handling failed (client=%s)',
                                   account.client.slug if account else '-', exc_info=True)
    return HttpResponse('ok')


def _handle_message(request, account, phone_number_id, message):
    wamid = str(message.get('id') or '')
    if not wamid:
        return
    # Idempotency BEFORE any effect: Meta redelivers while a slow turn is
    # still running, and a batch may be retried whole.
    if not _once(f'wa:msg:{wamid[:120]}', 86400):
        logger.info('WhatsApp duplicate message ignored')
        return
    if account is None:
        logger.warning('WhatsApp message for unknown or inactive phone_number_id=%s',
                       phone_number_id)
        return
    wa_id = str(message.get('from') or '').strip()
    if not wa_id.isdigit() or len(wa_id) > 20:
        logger.warning('WhatsApp message with a malformed sender on %s', account.client.slug)
        return
    client = account.client
    chash = contact_hash(wa_id)
    started = time.monotonic()
    outcome = _process(request, account, client, wa_id, chash, wamid, message)
    logger.info('wa turn client=%s hash=%s ms=%d outcome=%s', client.slug, chash[:8],
                int((time.monotonic() - started) * 1000), outcome)


def _process(request, account, client, wa_id, chash, wamid, message):
    """One inbound message of a known tenant, end to end. Returns the
    outcome label for the log."""
    day = _day()
    if _stale(message):
        return 'stale'
    if 'whatsapp' not in entitlements.entitlements(client).get('channels', ()):
        if _once(f'wa:noplan:{client.slug}:{day}'):
            logger.warning('WhatsApp message for %s but its plan does not include the channel',
                           client.slug)
        return 'no_plan'
    if client.clinic_mode:
        # Defense in depth (ChannelAccount.clean refuses clinic tenants): no
        # model, no Conversation, one fixed notice per contact and day.
        if _once(f'wa:clinic:{client.slug}:{chash}:{day}'):
            send_text(account, wa_id, _t(client, 'clinic'))
        return 'clinic'

    text, kind = extract_text(message)
    if kind == 'ignore' or (kind == 'text' and not text):
        return 'ignored'

    # Per-contact daily cap, counted BEFORE anything that sends (keywords
    # included, or STOP/START would be a free reply loop): one number cannot
    # spend the tenant's day or flood us with images. One notice per day.
    cap = int(getattr(settings, 'DOMINIO_WA_CONTACT_DAILY_CAP', 40) or 0)
    capped = bool(cap) and _counter_hit(f'wa:msgs:{client.slug}:{chash}:{day}', cap)
    if capped and _once(f'wa:capmsg:{client.slug}:{chash}:{day}'):
        send_text(account, wa_id, _t(client, 'cap'))
    if kind == 'text':
        # The opt-out record is kept even past the cap (it is a legal
        # record); only the confirmation is skipped.
        word = _keyword(text)
        if word in STOP_WORDS:
            ChannelOptOut.objects.get_or_create(client=client, channel='whatsapp',
                                                contact_hash=chash)
            if not capped:
                send_text(account, wa_id, _t(client, 'stop'))
            return 'stop'
        if word in START_WORDS:
            ChannelOptOut.objects.filter(client=client, channel='whatsapp',
                                         contact_hash=chash).delete()
            if not capped:
                send_text(account, wa_id, _t(client, 'start'))
            return 'start'
    if is_opted_out(client, chash):
        return 'opted_out'
    if capped:
        return 'contact_cap'
    if kind == 'media':
        send_text(account, wa_id, _t(client, 'media'))
        return 'media'

    # Tenant and global caps count model calls only, on their own counters
    # (the widget's 'chat:client:*' stays the widget's).
    if client.daily_message_cap and _counter_hit(f'chat:wa:{client.slug}:{day}',
                                                 client.daily_message_cap):
        logger.warning('Tenant WhatsApp quota reached for %s', client.slug)
        if _once(f'wa:busy:{client.slug}:{chash}:{day}'):
            send_text(account, wa_id, _t(client, 'busy'))
        return 'tenant_cap'
    if views._global_daily_cap_hit():
        if _once(f'wa:busy:{client.slug}:{chash}:{day}'):
            send_text(account, wa_id, _t(client, 'busy'))
        return 'global_cap'

    conversation = resolve_conversation(client, chash, wa_id=wa_id)
    handlers, profile = views.build_handlers(
        request, client, conversation, actor_key=f'wa:{chash}', channel='whatsapp',
        contact=wa_id)
    mark_read(account, wamid)
    try:
        result = run_turn(client, conversation, text, channel='whatsapp', handlers=handlers,
                          profile=profile, extra_context=['CHANNEL: whatsapp'],
                          timeout=TURN_TIMEOUT)
        reply, outcome = result.reply, 'ok'
    except agent.AgentNotConfigured:
        if _once('wa:noapikey', 3600):   # a misconfiguration, not one alert per message
            logger.error('WhatsApp turn for %s without ANTHROPIC_API_KEY', client.slug)
        reply, outcome = _t(client, 'error'), 'not_configured'
    except anthropic.APIError as exc:
        logger.warning('WhatsApp turn for %s failed: %s', client.slug, exc.__class__.__name__)
        reply, outcome = _t(client, 'error'), 'model_error'
    if not send_text(account, wa_id, reply):
        outcome += '_unsent'
    return outcome
