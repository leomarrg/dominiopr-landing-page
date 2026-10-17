"""SEO plumbing: robots.txt, sitemap.xml, web manifest, llms.txt, favicon.ico.

Public pages only. Everything under /dashboard/, /api/, /bienvenida/ and the
widget endpoint is disallowed for crawlers (private, transactional or
machine-only), and is deliberately absent from the sitemap.
"""
import datetime as dt
import functools
import os

from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.http import HttpResponse, HttpResponsePermanentRedirect, JsonResponse
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.http import require_GET

_PUBLIC_PAGES = [
    # (url name, changefreq, priority, template whose edits change the page)
    ('index', 'weekly', 1.0, 'landing/index.html'),
    ('get_started', 'weekly', 0.9, 'landing/get_started.html'),
    ('terms', 'yearly', 0.2, 'landing/terms.html'),
    ('privacy', 'yearly', 0.2, 'landing/privacy.html'),
]


@functools.lru_cache(maxsize=None)
def _template_mtime(name):
    """Last modification of a page's template (UTC), or None. A deploy only
    rewrites the files it changes, so this is "when the page last changed".
    Cached per process: a deploy restarts gunicorn anyway."""
    try:
        path = get_template(name).origin.name
        return dt.datetime.fromtimestamp(os.path.getmtime(path), tz=dt.timezone.utc)
    except (TemplateDoesNotExist, OSError, AttributeError, TypeError):
        return None


class PublicPagesSitemap(Sitemap):
    protocol = 'https'

    def items(self):
        return _PUBLIC_PAGES

    def location(self, item):
        return reverse(item[0])

    def changefreq(self, item):
        return item[1]

    def priority(self, item):
        return item[2]

    def lastmod(self, item):
        return _template_mtime(item[3])

    def get_urls(self, page=1, site=None, protocol=None):
        # Force the public domain (SITE_URL) rather than the request host so
        # sitemap entries always match the canonical URLs.
        class _Site:
            domain = settings.SITE_URL.split('://', 1)[-1].rstrip('/')
            name = 'DOMINIO'
        return super().get_urls(page=page, site=_Site(), protocol='https')


SITEMAPS = {'pages': PublicPagesSitemap}


@require_GET
def robots_txt(request):
    body = '\n'.join([
        'User-agent: *',
        'Allow: /',
        'Disallow: /dashboard/',
        'Disallow: /admin/',
        'Disallow: /api/',
        'Disallow: /bienvenida/',
        'Disallow: /confirmar/',
        'Disallow: /widget.js',
        '',
        f'Sitemap: {settings.SITE_URL}/sitemap.xml',
        '',
    ])
    resp = HttpResponse(body, content_type='text/plain; charset=utf-8')
    resp['Cache-Control'] = 'public, max-age=86400'
    return resp


@require_GET
def webmanifest(request):
    data = {
        'name': 'DOMINIO — Estudio de Software y Tecnología',
        'short_name': 'DOMINIO',
        'lang': 'es-PR',
        'start_url': '/',
        'display': 'browser',
        'background_color': '#0b1a2b',
        'theme_color': '#0b1a2b',
        'icons': [
            {'src': static('landing/images/brand/icon-192.png'), 'sizes': '192x192', 'type': 'image/png'},
            {'src': static('landing/images/brand/icon-512.png'), 'sizes': '512x512', 'type': 'image/png'},
        ],
    }
    resp = JsonResponse(data, content_type='application/manifest+json')
    resp['Cache-Control'] = 'public, max-age=86400'
    return resp


@require_GET
def favicon(request):
    """/favicon.ico -> the 32px PNG (permanent). Browsers, feed readers and
    crawlers request it regardless of the <link rel="icon"> tags."""
    resp = HttpResponsePermanentRedirect(static('landing/images/brand/favicon-32.png'))
    resp['Cache-Control'] = 'public, max-age=86400'
    return resp


def _price(amount):
    return f'${amount:,}' if amount is not None else 'a la medida'


@require_GET
def llms_txt(request):
    """/llms.txt (llmstxt.org): what DOMINIO is, in plain text, for AI answer
    engines. Only public facts; the plans come from views.PUBLIC_PLANS so the
    prices never drift from /get-started/."""
    from .views import PUBLIC_PLANS   # views imports a lot; only when asked

    base = settings.SITE_URL
    lines = [
        '# DOMINIO',
        '',
        '> DOMINIO es un estudio de software y tecnología en Puerto Rico. Diseña, '
        'construye y mantiene plataformas operacionales a la medida, dashboards, '
        'automatización con IA y agentes de chat para negocios.',
        '',
        '## Servicios',
        '',
        '- Plataformas operacionales a la medida',
        '- Dashboards y reportes',
        '- IA y automatización',
        '- Landing pages, formularios y campañas',
        '- Estrategia de producto y consultoría técnica',
        '- Gestión de tecnología y soporte de IT',
        '',
        '## DOMINIO Chat',
        '',
        'Agente de IA para la página web de un negocio (y WhatsApp como añadido): '
        'contesta las preguntas de los clientes 24/7 con la información del negocio, '
        'captura leads, agenda citas y avisa al equipo. DOMINIO lo instala, lo '
        'configura y lo mantiene; el negocio ve cada conversación y cada lead en '
        'un dashboard privado.',
        '',
        '## Planes',
        '',
    ]
    for plan in PUBLIC_PLANS:
        if plan.get('monthly') is not None:
            price = f"{_price(plan['monthly'])}/mes"
            if plan.get('setup') is not None:
                price += f", instalación {_price(plan['setup'])}"
        else:
            price = 'precio a la medida'
        lines.append(f"- {plan['name']} ({price}): {plan.get('tagline', '')}")
    lines += [
        '',
        'Precios mensuales en dólares (USD).',
        '',
        '## Enlaces',
        '',
        f'- [Inicio]({base}/)',
        f'- [Empezar y planes]({base}{reverse("get_started")})',
        f'- [Privacidad]({base}{reverse("privacy")})',
        f'- [Términos]({base}{reverse("terms")})',
        '',
        '## Contacto',
        '',
        '- Correo: hola@dominiopr.com',
        f'- Formulario: {base}/#contact',
        '',
    ]
    resp = HttpResponse('\n'.join(lines), content_type='text/plain; charset=utf-8')
    resp['Cache-Control'] = 'public, max-age=86400'
    return resp
