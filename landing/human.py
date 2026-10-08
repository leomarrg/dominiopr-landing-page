"""Human check (hCaptcha) for the chat widgets, the demo and the public forms.

OFF unless BOTH settings.HCAPTCHA_SITEKEY and settings.HCAPTCHA_SECRET are set:
with either one empty every caller skips the check and the site behaves
exactly as it did before this module existed.

Two pieces:

* `verify(token, remote_ip)` asks hCaptcha's siteverify whether a checkbox
  token is genuine. Fail-open only when hCaptcha cannot be reached (timeout,
  network/TLS error) or answers 5xx — it ACCEPTS and logs a warning, the same
  trade-off as landing/emailcheck: an outage at hCaptcha must not take the
  chat and every form down with it. Anything hCaptcha did answer (4xx, a body
  that is not the expected JSON, `success: false`, a token solved on a host
  we do not serve) rejects, and a malformed token never leaves the box.

* The "human pass" for the JSON chat APIs. Solving one challenge in the
  /verificar/ iframe yields a value signed with django.core.signing (salt
  'human-pass', valid HUMAN_PASS_TTL seconds) that is bound to the tenant slug
  AND to the widget's visitor id, so a pass farmed on one tenant or handed to
  another visitor is worthless. Each pass also carries a message budget
  (HUMAN_PASS_MAX_MESSAGES) counted in the cache under a hash of the pass, so
  one solved challenge cannot be replayed by a bot for thousands of messages.
"""

import hashlib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import JsonResponse
from django.http.request import validate_host

from .emailcheck import _tls_context

logger = logging.getLogger(__name__)

VERIFY_URL = 'https://api.hcaptcha.com/siteverify'
VERIFY_TIMEOUT = 5
SALT = 'human-pass'
# hCaptcha tokens are a few KB at most; anything larger is not a token.
MAX_TOKEN_CHARS = 8192
MAX_PASS_CHARS = 512
# Real tokens are base64url-ish ('P1_eyJ…' / 'E1_…'); the test sitekey returns
# '10000000-aaaa-bbbb-cccc-000000000001'. '+', '/' and '=' are tolerated in
# case of standard base64; whitespace, quotes or control chars never are.
_TOKEN_RE = re.compile(r'^[A-Za-z0-9_.:+/=\-]{20,%d}$' % MAX_TOKEN_CHARS)
# hCaptcha's published test secret (pairs with the 10000000-ffff-… sitekeys).
TEST_SECRET = '0x0000000000000000000000000000000000000000'

# The public demo on /get-started/ is not a tenant. Leading underscore on
# purpose: slugify() strips it, so no Client created through the app can ever
# share this key (and with it, its passes).
DEMO_KEY = '_demo'
# Keys whose challenge runs on DOMINIO's own pages (first party).
FIRST_PARTY_KEYS = frozenset({'dominio', DEMO_KEY})

FORM_ERROR = 'Confirma que no eres un robot.'
CHALLENGE_ERROR = 'Verifica que eres humano para continuar.'

HCAPTCHA_SOURCES = 'https://hcaptcha.com https://*.hcaptcha.com'

_VISITOR_RE = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
# What an allowed_origins entry may look like before it is written into a CSP
# header: a bare host with an optional port. Anything else (a ';', a space, a
# quote) could smuggle extra directives into the policy, so it is skipped.
_HOST_RE = re.compile(r'^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:\d{1,5})?$')
_LOCAL_HOSTS = frozenset({'localhost', '127.0.0.1'})


def enabled():
    return bool(getattr(settings, 'HCAPTCHA_SITEKEY', '')
                and getattr(settings, 'HCAPTCHA_SECRET', ''))


# ---- siteverify ---------------------------------------------------------

def _served_hosts():
    """Hostnames a genuine token may have been solved on: every challenge
    (the /verificar/ iframe, the forms, the admin login) renders on DOMINIO's
    own pages, so hCaptcha's `hostname` must be one of ours."""
    hosts = set()
    site = urlparse(getattr(settings, 'SITE_URL', '') or '').hostname
    if site:
        hosts.add(site.lower())
    for entry in getattr(settings, 'ALLOWED_HOSTS', []) or []:
        entry = str(entry).strip().lower()
        if entry:
            hosts.add(entry)
    if settings.DEBUG:
        hosts |= _LOCAL_HOSTS
    return hosts


def _hostname_ok(hostname):
    if not isinstance(hostname, str) or not hostname:
        return False
    return validate_host(hostname.strip().lower().rstrip('.'), list(_served_hosts()))


def _using_test_secret():
    return getattr(settings, 'HCAPTCHA_SECRET', '') == TEST_SECRET


def verify(token, remote_ip=None):
    """True when hCaptcha confirms the token, or cannot be reached at all.

    Fail-open ONLY on a network failure (timeout, DNS, connection refused,
    TLS) or a 5xx from hCaptcha: an outage there must not take the forms and
    the chat down. Anything hCaptcha actually answered — a 4xx, a body that
    is not the expected JSON, success:false, a token solved on a host we do
    not serve — rejects. A malformed token is rejected before any call."""
    token = token.strip() if isinstance(token, str) else ''
    if not _TOKEN_RE.match(token):
        return False
    data = {
        'secret': getattr(settings, 'HCAPTCHA_SECRET', ''),
        'response': token,
        'sitekey': getattr(settings, 'HCAPTCHA_SITEKEY', ''),
    }
    if remote_ip:
        data['remoteip'] = remote_ip
    req = urllib.request.Request(
        VERIFY_URL,
        data=urllib.parse.urlencode(data).encode('ascii'),
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'User-Agent': 'dominio-human-check/1'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=VERIFY_TIMEOUT,
                                    context=_tls_context()) as resp:
            body = resp.read(65536)
    except urllib.error.HTTPError as exc:
        # hCaptcha answered. A 5xx is their outage (fail open); a 4xx is a
        # rejection of this request.
        if exc.code >= 500:
            logger.warning('hCaptcha siteverify HTTP %s; failing open', exc.code)
            return True
        logger.info('hCaptcha siteverify HTTP %s; rejecting', exc.code)
        return False
    except (urllib.error.URLError, OSError) as exc:  # incl. socket.timeout
        logger.warning('hCaptcha siteverify unavailable (%s); failing open',
                       exc.__class__.__name__)
        return True
    try:
        result = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        logger.warning('hCaptcha siteverify returned an unparseable body; rejecting')
        return False
    if not isinstance(result, dict) or not isinstance(result.get('success'), bool):
        logger.warning('hCaptcha siteverify returned an unexpected body; rejecting')
        return False
    if result['success']:
        hostname = result.get('hostname')
        # Test keys report a placeholder hostname ('dummy-key-pass').
        if hostname is not None and not _using_test_secret() and not _hostname_ok(hostname):
            logger.warning('hCaptcha token solved on a host we do not serve: %s',
                           str(hostname)[:80])
            return False
        return True
    codes = result.get('error-codes') or []
    codes = [str(c)[:60] for c in codes] if isinstance(codes, list) else []
    if any(c in ('invalid-input-secret', 'sitekey-secret-mismatch', 'missing-input-secret')
           for c in codes):
        # Our configuration is wrong: every real human is being rejected.
        logger.error('hCaptcha rejects our own keys (%s) — check HCAPTCHA_SITEKEY/SECRET',
                     ', '.join(codes))
    else:
        logger.info('hCaptcha rejected a token: %s', ', '.join(codes) or 'no reason')
    return False


# ---- human pass ---------------------------------------------------------

def clean_visitor(value):
    """The widget's visitor id, or '' when it is not one we could have made."""
    if not isinstance(value, str):
        return ''
    value = value.strip()
    return value if _VISITOR_RE.match(value) else ''


def issue_pass(slug, visitor):
    return signing.dumps({'s': slug, 'v': visitor}, salt=SALT)


def _counter_key(value):
    return 'humanpass:' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:40]


def check_pass(value, slug, visitor):
    """Valid, unexpired, bound to this slug + visitor, and still within its
    message budget. Counts one message against the budget when it passes."""
    if not isinstance(value, str) or not value or len(value) > MAX_PASS_CHARS:
        return False
    visitor = clean_visitor(visitor)
    if not visitor:
        return False
    ttl = int(getattr(settings, 'HUMAN_PASS_TTL', 7200))
    try:
        data = signing.loads(value, salt=SALT, max_age=ttl)
    except signing.BadSignature:  # includes SignatureExpired
        return False
    if not isinstance(data, dict) or data.get('s') != slug or data.get('v') != visitor:
        return False
    cap = int(getattr(settings, 'HUMAN_PASS_MAX_MESSAGES', 30))
    key = _counter_key(value)
    # add-then-incr: concurrent requests replaying one pass each get a
    # distinct count (a get-then-set would let a burst all read the same
    # value and overspend the budget). The pass cannot outlive its TTL, so
    # neither needs its counter.
    cache.add(key, 0, ttl)
    try:
        used = cache.incr(key)
    except ValueError:  # culled/expired between add and incr
        cache.add(key, 0, ttl)
        try:
            used = cache.incr(key)
        except ValueError:
            return False
    return used <= cap


def challenge_response():
    """403 the widgets recognise as 'open the challenge, then resend'."""
    return JsonResponse({'error': CHALLENGE_ERROR, 'challenge': True}, status=403)


# ---- /verificar/ origin rules -------------------------------------------

def normalize_origin(raw):
    """'scheme://host[:port]' or '' — never a path, query or credentials,
    because the value becomes a postMessage targetOrigin and a CSP source."""
    if not isinstance(raw, str) or not raw or len(raw) > 200:
        return ''
    try:
        p = urlparse(raw.strip())
        port = p.port  # raises ValueError on a malformed port
    except ValueError:
        return ''
    if p.scheme not in ('http', 'https') or not p.hostname:
        return ''
    if p.username or p.password or p.query or p.fragment or p.path not in ('', '/'):
        return ''
    host = p.hostname.lower()
    if not _HOST_RE.match(host):
        return ''
    netloc = f'{host}:{port}' if port else host
    return f'{p.scheme}://{netloc}'


def _first_party_origins(request):
    origins = set()
    site = normalize_origin(getattr(settings, 'SITE_URL', ''))
    if site:
        origins.add(site)
    # The host serving this request already passed ALLOWED_HOSTS.
    own = normalize_origin(request.build_absolute_uri('/'))
    if own:
        origins.add(own)
    return origins


def frame_ancestors(key, client, origin, request):
    """The CSP frame-ancestors sources for the challenge page, or None when
    `origin` may not receive a pass for `key`.

    Same rule as chat_api's origin allowlist: a tenant with allowed_origins
    is limited to those hosts; a tenant without one accepts any https site
    (its chat does too). DOMINIO's own keys accept DOMINIO's own site, plus
    localhost while DEBUG is on.
    """
    if not origin:
        return None
    p = urlparse(origin)
    host = (p.hostname or '').lower()
    netloc = p.netloc.lower()
    local_dev = bool(settings.DEBUG) and host in _LOCAL_HOSTS

    hosts = client.origin_list() if client is not None and client.allowed_origins else []
    safe_hosts = [h for h in hosts if _HOST_RE.match(h)]

    if key in FIRST_PARTY_KEYS:
        allowed = _first_party_origins(request)
        ok = (origin in allowed or local_dev
              or netloc in hosts or host in hosts)
        sources = sorted(allowed) + [f'https://{h}' for h in safe_hosts]
    elif hosts:
        ok = netloc in hosts or host in hosts
        sources = [f'https://{h}' for h in safe_hosts]
    else:
        ok = p.scheme == 'https' or local_dev
        sources = ['https:']

    if not ok:
        return None
    result = ["'self'"]
    for src in sources + [origin]:
        if src not in result and not (src.startswith('https://') and 'https:' in result):
            result.append(src)
    return result


def challenge_csp(ancestors, nonce):
    """The challenge page's own policy (the CSP middleware uses setdefault,
    so this one wins). Scripts: only ours (nonce) and hCaptcha's."""
    return (
        "default-src 'self'; "
        f"script-src 'nonce-{nonce}' {HCAPTCHA_SOURCES}; "
        f"style-src 'self' 'unsafe-inline' {HCAPTCHA_SOURCES}; "
        f"frame-src {HCAPTCHA_SOURCES}; "
        f"connect-src 'self' {HCAPTCHA_SOURCES}; "
        "img-src 'self' data: https:; "
        "font-src 'self'; "
        "form-action 'self'; "
        f"frame-ancestors {' '.join(ancestors)}; "
        "base-uri 'none'; "
        "object-src 'none'"
    )
