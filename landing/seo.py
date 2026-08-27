"""SEO plumbing: robots.txt, sitemap.xml, web manifest.

Public pages only. Everything under /dashboard/, /api/, /bienvenida/ and the
widget endpoint is disallowed for crawlers (private, transactional or
machine-only), and is deliberately absent from the sitemap.
"""
from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET

_PUBLIC_PAGES = [
    # (url name, changefreq, priority)
    ('index', 'weekly', 1.0),
    ('get_started', 'weekly', 0.9),
    ('terms', 'yearly', 0.2),
    ('privacy', 'yearly', 0.2),
]


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
    from django.templatetags.static import static
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
