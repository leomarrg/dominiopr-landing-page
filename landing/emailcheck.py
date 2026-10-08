"""Is this a real, deliverable email address? (leads and bookings)

`check_email(addr)` -> (ok, reason). Three layers, cheapest first:

1. Syntax (Django's validate_email).
2. Disposable-domain blocklist (landing/data/disposable_domains.txt), matching
   the domain and every parent domain.
3. When settings.DOMINIO_EMAIL_DNS_CHECK is on: does the domain exist and
   accept mail? Asked over DNS-over-HTTPS with stdlib urllib (no new
   dependency, no raw UDP from the box): MX first; NXDOMAIN or a null MX
   (RFC 7505, "0 .") rejects; no MX falls back to A/AAAA (RFC 5321 implicit
   MX), and a domain with none of them rejects.

Fail-open by design: a timeout, HTTP error or unparseable answer ACCEPTS the
address and logs a warning. A real lead must never be lost because a
resolver was slow. Verdicts are cached per domain for a day; failures only
briefly, so a resolver hiccup is retried soon instead of waving every address
of that domain through for 24 h.

`reason` is a Spanish, visitor-facing sentence (shown in forms and handed to
the chat agent so it can re-ask).
"""

import hashlib
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from django.conf import settings
from django.core.cache import caches
from django.core.exceptions import ValidationError
from django.core.validators import validate_email


_TLS_CONTEXT = None


def _tls_context():
    """System trust store plus certifi's bundle when available: some Python
    builds (e.g. Windows) miss roots the DoH endpoint chains to, and a failed
    TLS check would silently fail open on every lookup."""
    global _TLS_CONTEXT
    if _TLS_CONTEXT is None:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:  # certifi missing: system store only
            pass
        _TLS_CONTEXT = ctx
    return _TLS_CONTEXT

logger = logging.getLogger(__name__)

INVALID_REASON = 'Escribe un correo válido.'
DISPOSABLE_REASON = 'Ese correo parece temporal — usa uno permanente.'
UNDELIVERABLE_REASON = ('No encontramos ese dominio de correo — revisa que esté '
                        'bien escrito.')

DOH_URL = 'https://cloudflare-dns.com/dns-query'
# Per-request timeout, and a ceiling for the whole MX -> A -> AAAA chain so a
# slow resolver costs a form submit at most a few seconds.
DNS_TIMEOUT = 3.0
DNS_TOTAL_BUDGET = 4.0
CACHE_TTL = 60 * 60 * 24
FAILURE_TTL = 60 * 5

# DNS RR type codes in the DoH JSON answer.
_RR_A, _RR_MX, _RR_AAAA = 1, 15, 28
_NOERROR, _NXDOMAIN = 0, 3

_DATA_FILE = Path(__file__).resolve().parent / 'data' / 'disposable_domains.txt'
_disposable = None
_disposable_lock = threading.Lock()


class _Unknown(Exception):
    """The resolver could not give a usable answer: fail open."""


def disposable_domains():
    """The blocklist as a frozenset, read once per process."""
    global _disposable
    if _disposable is None:
        with _disposable_lock:
            if _disposable is None:
                try:
                    lines = _DATA_FILE.read_text(encoding='utf-8').splitlines()
                except OSError:
                    logger.exception('Disposable-domain list missing: %s', _DATA_FILE)
                    lines = []
                _disposable = frozenset(
                    ln.strip().lower() for ln in lines
                    if ln.strip() and not ln.lstrip().startswith('#'))
    return _disposable


def is_disposable(domain):
    """True when `domain` or any parent domain is on the blocklist
    (x.mailinator.com matches mailinator.com)."""
    labels = (domain or '').lower().rstrip('.').split('.')
    blocked = disposable_domains()
    return any('.'.join(labels[i:]) in blocked for i in range(len(labels) - 1))


def _ascii_domain(domain):
    """IDNA-encode a Unicode domain for the DNS query; None if it can't be."""
    domain = (domain or '').strip().lower().rstrip('.')
    try:
        ascii_domain = domain.encode('idna').decode('ascii')
    except UnicodeError:
        return None
    return ascii_domain if 0 < len(ascii_domain) <= 253 else None


def _doh(name, rtype, deadline):
    """One DoH JSON query. Raises _Unknown on any transport/parse problem."""
    remaining = deadline - time.monotonic()
    if remaining <= 0.2:
        raise _Unknown('time budget exhausted')
    url = DOH_URL + '?' + urllib.parse.urlencode({'name': name, 'type': rtype})
    req = urllib.request.Request(url, headers={'accept': 'application/dns-json'})
    try:
        with urllib.request.urlopen(req, timeout=min(DNS_TIMEOUT, remaining),
                                    context=_tls_context()) as resp:
            data = json.loads(resp.read(65536).decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # URLError/HTTPError, socket timeout, bad JSON/UTF-8.
        raise _Unknown(f'{type(exc).__name__}: {exc}') from exc
    if not isinstance(data, dict) or not isinstance(data.get('Status'), int):
        raise _Unknown('unexpected DoH payload')
    return data


def _answers(data, rr_type):
    return [a for a in (data.get('Answer') or [])
            if isinstance(a, dict) and a.get('type') == rr_type]


def _lookup(ascii_domain):
    """'ok' or 'bad' for a domain; raises _Unknown when DNS can't tell."""
    deadline = time.monotonic() + DNS_TOTAL_BUDGET
    mx = _doh(ascii_domain, 'MX', deadline)
    if mx['Status'] == _NXDOMAIN:
        return 'bad'
    if mx['Status'] != _NOERROR:
        # SERVFAIL (often broken DNSSEC), REFUSED...: not proof of anything.
        raise _Unknown(f'MX status {mx["Status"]}')
    records = _answers(mx, _RR_MX)
    if records:
        exchanges = [str(r.get('data') or '').split() for r in records]
        # RFC 7505 null MX: a single "0 ." record = "this domain takes no mail".
        if all(len(e) == 2 and e[0] == '0' and e[1] == '.' for e in exchanges):
            return 'bad'
        return 'ok'
    # No MX: RFC 5321 falls back to the A/AAAA record (implicit MX).
    for rtype, rr in (('A', _RR_A), ('AAAA', _RR_AAAA)):
        data = _doh(ascii_domain, rtype, deadline)
        if data['Status'] == _NXDOMAIN:
            return 'bad'
        if data['Status'] != _NOERROR:
            raise _Unknown(f'{rtype} status {data["Status"]}')
        if _answers(data, rr):
            return 'ok'
    return 'bad'


def _cache_key(ascii_domain):
    key = 'emaildns:' + ascii_domain
    # Keep under the cache backend's 250-char key warning for absurd domains.
    if len(key) > 200:
        key = 'emaildns:h:' + hashlib.sha256(ascii_domain.encode()).hexdigest()
    return key


def domain_status(domain):
    """'ok', 'bad' or '' (unknown -> caller fails open). Cached per domain."""
    ascii_domain = _ascii_domain(domain)
    if ascii_domain is None:
        logger.warning('Email DNS check skipped: cannot IDNA-encode a domain')
        return ''
    key = _cache_key(ascii_domain)
    cache = caches['emaildns']
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        status = _lookup(ascii_domain)
    except _Unknown as exc:
        logger.warning('Email DNS check failed open for %s: %s', ascii_domain, exc)
        cache.set(key, '', FAILURE_TTL)
        return ''
    except Exception:  # never let a bug here cost a lead
        logger.exception('Email DNS check crashed for %s; failing open', ascii_domain)
        cache.set(key, '', FAILURE_TTL)
        return ''
    cache.set(key, status, CACHE_TTL)
    return status


def check_email(addr):
    """(ok, reason). `reason` is '' when ok, else a Spanish visitor-facing
    sentence. Fails open on any DNS/network problem."""
    addr = (addr or '').strip()
    if not addr:
        return False, INVALID_REASON
    try:
        validate_email(addr)
    except ValidationError:
        return False, INVALID_REASON
    domain = addr.rsplit('@', 1)[1].lower().rstrip('.')
    if is_disposable(domain) or is_disposable(_ascii_domain(domain) or ''):
        return False, DISPOSABLE_REASON
    if not getattr(settings, 'DOMINIO_EMAIL_DNS_CHECK', False):
        return True, ''
    if domain_status(domain) == 'bad':
        return False, UNDELIVERABLE_REASON
    return True, ''
