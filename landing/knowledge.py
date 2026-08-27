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
import ipaddress
import logging
import re
import socket
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlparse

from .models import KnowledgeFragment

logger = logging.getLogger(__name__)

MAX_FETCH_BYTES = 500 * 1024
MAX_TEXT_CHARS = 30000
FRAGMENT_CHARS = 2000
TIMEOUT = 12


class _TextExtractor(HTMLParser):
    """Strip tags, drop script/style/nav noise, keep readable text."""
    SKIP = {'script', 'style', 'noscript', 'svg', 'iframe', 'head'}

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would re-resolve the host AFTER our SSRF check, so a public
    URL could bounce us to 169.254.169.254. Refuse instead of re-validating."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('La URL redirige a otra página; usa la dirección final.')


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
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError('Esa dirección no es pública.')


def fetch_url_text(url):
    """Fetch a public http(s) page and return its readable text. Raises
    ValueError with a human-readable message on anything invalid."""
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

    req = urllib.request.Request(url, headers={'User-Agent': 'DOMINIO-Chat-24-7 knowledge bot'})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            ctype = resp.headers.get('Content-Type', '')
            if not any(t in ctype for t in ('text/html', 'text/plain', 'application/xhtml')):
                raise ValueError(f'Tipo de contenido no soportado: {ctype[:60]}')
            raw = resp.read(MAX_FETCH_BYTES + 1)
    except ValueError:
        raise
    except Exception as e:
        logger.warning('knowledge fetch failed for %s: %s', url, e)
        raise ValueError('No se pudo acceder a la URL.')
    if len(raw) > MAX_FETCH_BYTES:
        raise ValueError('La página es demasiado grande (máx. 500 KB).')
    text_html = raw.decode('utf-8', errors='replace')
    if 'text/plain' in ctype:
        text = text_html
    else:
        extractor = _TextExtractor()
        extractor.feed(text_html)
        text = '\n'.join(extractor.parts)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if not text:
        raise ValueError('La página no contiene texto legible.')
    return text[:MAX_TEXT_CHARS]


def chunk_text(text, size=FRAGMENT_CHARS):
    return [text[i:i + size] for i in range(0, len(text), size)]


def process_source(source):
    """Validate + extract a source into fragments. Sets status to 'active' on
    success or 'error' (with the message) on failure. Returns the source."""
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
        source.status = 'active'
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
