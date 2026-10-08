"""
M-15 — knowledge source processing.

A source is validated and extracted into clean fragments; only ACTIVE sources
participate in the agent's compiled prompt (see Client.compiled_prompt). A
source that fails validation lands in status 'error' and never contaminates
answers — visible, excluded, re-processable.

URL fetching uses the stdlib with hard caps (size, timeout, content type) and
a plain HTML→text extraction. No vector index: fragments are concatenated into
the prompt until a tenant's knowledge outgrows the context budget (documented
decision in Phase 2).
"""
import html
import http.client
import ipaddress
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .models import KnowledgeFragment

logger = logging.getLogger(__name__)

MAX_FETCH_BYTES = 500 * 1024
MAX_TEXT_CHARS = 30000
FRAGMENT_CHARS = 2000
TIMEOUT = 12            # per socket operation (connect, one recv)
FETCH_DEADLINE = 20.0   # whole request, headers + body
READ_CHUNK = 65536


class _TextExtractor(HTMLParser):
    """Strip tags, drop script/style/nav noise, keep readable text."""
    SKIP = {'script', 'style', 'noscript', 'svg', 'iframe', 'head'}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts = []
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == 'a' and len(self.hrefs) < 500:
            href = dict(attrs).get('href')
            if href:
                self.hrefs.append(href.strip())

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


class RedirectRefused(ValueError):
    """Raised instead of following a redirect. `location` lets the site crawler
    queue the target as a NEW request that goes through every check again."""

    def __init__(self, location=''):
        super().__init__('La URL redirige a otra página; usa la dirección final.')
        self.location = location


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would re-resolve the host AFTER our SSRF check, so a public
    URL could bounce us to 169.254.169.254. Refuse instead of re-validating."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RedirectRefused(newurl)


# IPv6 prefixes that embed an IPv4 address and are routed to it by a
# translator/relay (NAT64, 6to4). ipaddress reports them as global, but
# 64:ff9b::a9fe:a9fe is 169.254.169.254 on a host with a NAT64 gateway.
_EMBEDDED_V4_NETS = (
    ipaddress.ip_network('64:ff9b::/96'),
    ipaddress.ip_network('64:ff9b:1::/48'),
    ipaddress.ip_network('2002::/16'),
)


def _assert_public_ip(value):
    ip = ipaddress.ip_address(str(value).split('%', 1)[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.version == 6 and any(ip in net for net in _EMBEDDED_V4_NETS):
        raise ValueError('Esa dirección no es pública.')
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified or not ip.is_global):
        raise ValueError('Esa dirección no es pública.')


# Sockets opened by the fetch running on this thread, so its watchdog can cut
# them when the TOTAL deadline passes (see fetch_resource).
_ACTIVE = threading.local()


def _guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                               source_address=None, *args, **kwargs):
    """Check the address we ACTUALLY connected to. _assert_public_host runs on
    a first DNS lookup; the connection does a second one, so a domain with a
    short TTL could answer public then private (DNS rebinding)."""
    sock = socket.create_connection(address, timeout, source_address)
    try:
        _assert_public_ip(sock.getpeername()[0])
    except ValueError:
        sock.close()
        raise
    tracked = getattr(_ACTIVE, 'socks', None)
    if tracked is not None:
        tracked.append(sock)
    return sock


def _cut(socks, flag):
    """Watchdog: shut the fetch's sockets down so a blocked recv returns now."""
    flag.append(True)
    for sock in list(socks):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._context)


def _opener():
    # ProxyHandler({}): an env proxy would make the peer check meaningless.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _GuardedHTTPHandler, _GuardedHTTPSHandler,
        _NoRedirect)


def _assert_public_host(host):
    """Block private/loopback/link-local targets BEFORE connecting.

    This endpoint is reachable by any paying customer, and the server sits on a
    cloud instance: without this, a knowledge source pointed at
    http://169.254.169.254/… or http://127.0.0.1:PORT/ turns the fetcher into a
    proxy into our own network, with the result readable back through the
    agent's compiled prompt.
    """
    if not host:
        raise ValueError('La URL no tiene dominio.')
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError('No se pudo resolver el dominio.')
    for *_ , sockaddr in infos:
        _assert_public_ip(sockaddr[0])


USER_AGENT = 'DOMINIO-Chat-24-7 knowledge bot'
PAGE_TYPES = ('text/html', 'text/plain', 'application/xhtml')
HTML_TYPES = ('text/html', 'application/xhtml')


def validate_public_url(url):
    """Scheme/port/host checks shared by every fetch. Returns the parsed URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('La URL debe empezar con http:// o https://')
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('El puerto de la URL no es válido.')
    if port not in (None, 80, 443):
        raise ValueError('Solo se permiten los puertos 80 y 443.')
    _assert_public_host(parsed.hostname)
    return parsed


def _read_bounded(resp, max_bytes, ends_at):
    """Body up to max_bytes + 1, giving up at `ends_at` (monotonic). The socket
    timeout only bounds ONE recv: a server dripping a byte every few seconds
    would otherwise hold the caller for as long as it likes. read1() returns
    after a single underlying read, so the clock is checked between drips;
    the worst overshoot is one socket TIMEOUT."""
    read = getattr(resp, 'read1', None) or resp.read
    chunks, size = [], 0
    while size <= max_bytes:
        if time.monotonic() >= ends_at:
            raise ValueError('La página tardó demasiado.')
        chunk = read(min(READ_CHUNK, max_bytes + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b''.join(chunks)


def fetch_resource(url, allowed_types=PAGE_TYPES, max_bytes=MAX_FETCH_BYTES, deadline=None):
    """GET a public http(s) URL with every guard on: public host (before and
    at connect time), ports 80/443, no redirects, timeout, size cap, content
    type allowlist, and a TOTAL time limit for the whole request: `deadline`
    seconds (the crawler passes what is left of its budget), never more than
    FETCH_DEADLINE. Returns (content_type, raw_bytes); raises ValueError with
    a message safe to show the tenant (RedirectRefused on a 3xx)."""
    validate_public_url(url)
    allowed = FETCH_DEADLINE if deadline is None else min(float(deadline), FETCH_DEADLINE)
    if allowed <= 0:
        raise ValueError('La página tardó demasiado.')
    ends_at = time.monotonic() + allowed
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    # The read loop checks the clock only for the BODY: connect, TLS and the
    # status/header lines are read by http.client with just a per-recv timeout,
    # so a server dripping header bytes could hold this thread indefinitely.
    # A watchdog shuts the socket at the deadline, whatever phase we're in.
    socks, timed_out = [], []
    _ACTIVE.socks = socks
    watchdog = threading.Timer(allowed, _cut, args=(socks, timed_out))
    watchdog.daemon = True
    watchdog.start()
    try:
        with _opener().open(req, timeout=max(1.0, min(TIMEOUT, allowed))) as resp:
            ctype = resp.headers.get('Content-Type', '') or ''
            if not any(t in ctype for t in allowed_types):
                raise ValueError(f'Tipo de contenido no soportado: {ctype[:60]}')
            raw = _read_bounded(resp, max_bytes, ends_at)
    except ValueError:
        raise
    except Exception as e:
        if timed_out:
            raise ValueError('La página tardó demasiado.')
        # A 404 is routine for the crawler (robots.txt, sitemap.xml).
        log = logger.info if isinstance(e, urllib.error.HTTPError) else logger.warning
        log('knowledge fetch failed for %s: %s', url, e)
        raise ValueError('No se pudo acceder a la URL.')
    finally:
        watchdog.cancel()
        _ACTIVE.socks = None
    if len(raw) > max_bytes:
        raise ValueError(f'La página es demasiado grande (máx. {max_bytes // 1024} KB).')
    return ctype, raw


def _clean(text):
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def html_to_text_and_links(text_html, base_url=''):
    """(readable text, absolute hrefs) from an HTML document."""
    extractor = _TextExtractor()
    extractor.feed(text_html)
    links = [urljoin(base_url, h) for h in extractor.hrefs] if base_url else extractor.hrefs
    return _clean('\n'.join(extractor.parts))[:MAX_TEXT_CHARS], links


def fetch_page(url):
    """One HTML page for the site crawler: (text, links). Non-HTML -> ValueError."""
    _ctype, raw = fetch_resource(url, allowed_types=HTML_TYPES)
    return html_to_text_and_links(raw.decode('utf-8', errors='replace'), base_url=url)


def fetch_url_text(url):
    """Fetch a public http(s) page and return its readable text. Raises
    ValueError with a human-readable message on anything invalid."""
    ctype, raw = fetch_resource(url)
    text_html = raw.decode('utf-8', errors='replace')
    if 'text/plain' in ctype:
        text = _clean(text_html)
    else:
        text, _links = html_to_text_and_links(text_html)
    if not text:
        raise ValueError('La página no contiene texto legible.')
    return text[:MAX_TEXT_CHARS]


def chunk_text(text, size=FRAGMENT_CHARS):
    return [text[i:i + size] for i in range(0, len(text), size)]


def process_source(source, success_status='active'):
    """Validate + extract a source into fragments. Sets status to
    `success_status` on success or 'error' (with the message) on failure.
    The dashboard passes 'draft' so a source never goes live, even for an
    instant, before it is deliberately activated. Returns the source."""
    try:
        if source.kind == 'url':
            text = fetch_url_text((source.origin or '').strip())
        else:  # 'text' / 'faq': the content field IS the knowledge
            text = (source.content or '').strip()
            if not text:
                raise ValueError('El contenido está vacío.')
        source.fragments.all().delete()
        KnowledgeFragment.objects.bulk_create([
            KnowledgeFragment(source=source, content=chunk, position=i)
            for i, chunk in enumerate(chunk_text(text))
        ])
        source.status = success_status
        source.error = ''
    except ValueError as e:
        source.status = 'error'
        source.error = str(e)[:300]
    except Exception as e:
        logger.exception('Knowledge processing failed for source %s', source.pk)
        source.status = 'error'
        source.error = f'Error inesperado: {e}'[:300]
    source.save(update_fields=['status', 'error', 'updated_at'])
    return source
