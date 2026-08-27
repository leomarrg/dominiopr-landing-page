"""
Create the DOMINIO catalog in Stripe from the code's own pricing table.

Rationale: the 6 products / 9 prices in docs/lanzamiento/01-stripe.md are
transcribed by hand into the dashboard, twice (test and live). That is where
amounts drift from `views.PLANS` and where a yearly price sneaks in as monthly.
This command reads PLANS and writes the catalog through the API, so the site
and Stripe cannot disagree.

Idempotent: products use deterministic ids (`dominio_starter`, ...) and prices
use lookup keys (`starter-monthly`, ...). Re-running finds what exists instead
of duplicating it. Prices in Stripe are immutable, so a changed amount creates
a NEW price and the old one is left alone (never archived automatically) —
the command says so when it happens.

    python manage.py stripe_bootstrap --dry-run
    python manage.py stripe_bootstrap
    python manage.py stripe_bootstrap --webhook-url https://dominiopr.com/api/stripe/webhook/
    python manage.py stripe_bootstrap --coupon LANZAMIENTO --coupon-percent 20 --coupon-months 3

Prints the `.env` block to paste on the server. Nothing here touches the
customer portal, the account branding or the automatic emails: those are
dashboard-only screens (sections 1, 5 and 6 of the guide).
"""
import urllib.parse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from landing import payments
from landing.views import PLAN_BY_ID

PAID_PLANS = ['starter', 'pro', 'scale']

WEBHOOK_EVENTS = [
    'checkout.session.completed',
    # Delayed methods (ACH, some cards) land here, not on .completed. The
    # handler treats it the same; without it those payments never provision.
    'checkout.session.async_payment_succeeded',
    'customer.subscription.updated',
    'customer.subscription.deleted',
]

OK, WARN = '  OK ', ' WARN'


class Command(BaseCommand):
    help = 'Create/verify the DOMINIO products, prices, webhook and coupon in Stripe.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be created; write nothing.')
        parser.add_argument('--yes', action='store_true',
                            help='Required to write against a live key (sk_live_).')
        parser.add_argument('--webhook-url', default='',
                            help='Create the webhook endpoint and print its signing secret.')
        parser.add_argument('--coupon', default='',
                            help='Promotion code to create, e.g. LANZAMIENTO.')
        parser.add_argument('--coupon-percent', type=int, default=20)
        parser.add_argument('--coupon-months', type=int, default=3,
                            help='0 = the discount applies once.')
        parser.add_argument('--tax-rate', type=float, default=0.0,
                            help='Create an EXCLUSIVE tax rate at this percent '
                                 '(e.g. 4 or 11.5) and print its id for the .env.')
        parser.add_argument('--tax-name', default='IVU',
                            help='Label the customer sees on the invoice (default: IVU).')
        parser.add_argument('--tax-country', default='PR',
                            help='ISO country for the tax rate (default: PR).')

    # -- helpers ---------------------------------------------------------
    def _line(self, status, label, detail=''):
        self.stdout.write(f'[{status}] {label}' + (f' - {detail}' if detail else ''))

    def _get(self, path):
        """GET that turns a 'No such ...' into None instead of raising."""
        try:
            return payments._request('GET', path)
        except payments.StripeError as e:
            if 'no such' in str(e).lower():
                return None
            raise

    # -- products / prices -----------------------------------------------
    def _product(self, pid, name, description):
        found = self._get('/v1/products/' + urllib.parse.quote(pid, safe=''))
        if found:
            if not found.get('active'):
                if not self.dry:
                    payments._request('POST', f'/v1/products/{pid}', {'active': 'true'})
                self._line(WARN, name, f'{pid} estaba archivado - reactivado')
            else:
                self._line(OK, name, f'ya existe [{pid}]')
            return found
        if self.dry:
            self._line(WARN, name, f'se crearia [{pid}]')
            return {'id': pid}
        created = payments._request('POST', '/v1/products', {
            'id': pid, 'name': name, 'description': description,
        })
        self._line(OK, name, f'creado [{pid}]')
        return created

    def _price(self, product_id, lookup_key, amount_usd, interval, label):
        """One price, addressed by lookup key. `interval` None = one-time."""
        cents = int(round(amount_usd * 100))
        q = '/v1/prices?limit=1&lookup_keys[]=' + urllib.parse.quote(lookup_key, safe='')
        existing = (payments._request('GET', q).get('data') or [None])[0]
        if existing:
            rec = existing.get('recurring') or {}
            matches = (existing.get('unit_amount') == cents
                       and rec.get('interval') == interval
                       and (rec.get('interval_count') or 1) == 1
                       and existing.get('active'))
            if matches:
                # tax_behavior can be set exactly once, while it is still
                # 'unspecified'. Prices created before we charged IVU are in
                # that state, so fix them in place instead of reissuing.
                if existing.get('tax_behavior') == 'unspecified' and not self.dry:
                    payments._request('POST', f'/v1/prices/{existing["id"]}',
                                      {'tax_behavior': 'exclusive'})
                    self._line(OK, label,
                               f'${amount_usd:,} [{existing["id"]}] - tax_behavior -> exclusive')
                    return existing['id']
                self._line(OK, label, f'${amount_usd:,} [{existing["id"]}]')
                return existing['id']
            # Prices are immutable: move the lookup key onto a fresh one.
            self._line(WARN, label,
                       f'el precio existente no cuadra ({existing.get("unit_amount")} '
                       f'centavos, {rec.get("interval") or "una vez"}) - se crea uno nuevo; '
                       f'el viejo [{existing["id"]}] queda sin usar, archivalo a mano')
        if self.dry:
            self._line(WARN, label, f'se crearia ${amount_usd:,}')
            return f'price_DRYRUN_{lookup_key}'
        data = {
            'product': product_id, 'currency': 'usd', 'unit_amount': str(cents),
            'lookup_key': lookup_key, 'transfer_lookup_key': 'true',
            'nickname': lookup_key,
            # The site advertises prices without tax, so the IVU is added on
            # top. Say so on the Price itself: 'unspecified' is what makes
            # Stripe Tax refuse to calculate later.
            'tax_behavior': 'exclusive',
        }
        if interval:
            data['recurring[interval]'] = interval
        created = payments._request('POST', '/v1/prices', data)
        self._line(OK, label, f'${amount_usd:,} creado [{created["id"]}]')
        return created['id']

    # -- webhook / coupon -------------------------------------------------
    def _webhook(self, url):
        """Returns the signing secret, only available the moment it's created."""
        for ep in payments._request('GET', '/v1/webhook_endpoints?limit=100').get('data', []):
            if ep.get('url') != url:
                continue
            missing = sorted(set(WEBHOOK_EVENTS) - set(ep.get('enabled_events') or []))
            if missing and not self.dry:
                payments._request('POST', f'/v1/webhook_endpoints/{ep["id"]}', {
                    f'enabled_events[{i}]': e for i, e in enumerate(WEBHOOK_EVENTS)})
                self._line(OK, 'webhook', f'eventos corregidos (faltaban {", ".join(missing)})')
            else:
                self._line(OK, 'webhook', f'ya existe [{ep["id"]}]')
            self._line(WARN, 'STRIPE_WEBHOOK_SECRET',
                       'el secret solo se muestra al crear el endpoint; si no lo tienes, '
                       'revelalo en Developers -> Webhooks -> este endpoint')
            return ''
        if self.dry:
            self._line(WARN, 'webhook', f'se crearia {url}')
            return ''
        data = {'url': url, 'description': 'DOMINIO - activacion de clientes'}
        data.update({f'enabled_events[{i}]': e for i, e in enumerate(WEBHOOK_EVENTS)})
        ep = payments._request('POST', '/v1/webhook_endpoints', data)
        self._line(OK, 'webhook', f'creado [{ep["id"]}]')
        return ep.get('secret', '')

    def _tax_rate(self, percent, name, country):
        """One EXCLUSIVE tax rate, reused if an identical one already exists.

        Tax rates are immutable in Stripe (only the display fields can change),
        so a different percentage means a different rate — the old one keeps
        applying to subscriptions already created with it, which is correct:
        changing IVU must not silently re-rate what a customer already signed.
        """
        for tr in payments._request('GET', '/v1/tax_rates?limit=100&active=true').get('data', []):
            same = (not tr.get('inclusive')
                    and abs(float(tr.get('percentage') or 0) - percent) < 0.001
                    and (tr.get('country') or '') == country)
            if same:
                self._line(OK, f'tax rate {percent}%', f'ya existe [{tr["id"]}]')
                return tr['id']
        if self.dry:
            self._line(WARN, f'tax rate {percent}%', f'se crearia ({name}, {country}, exclusive)')
            return ''
        tr = payments._request('POST', '/v1/tax_rates', {
            'display_name': name,
            'percentage': str(percent),
            'inclusive': 'false',
            'country': country,
            'description': f'{name} {percent}% - se suma al precio anunciado',
        })
        self._line(OK, f'tax rate {percent}%', f'creado [{tr["id"]}]')
        return tr['id']

    def _coupon(self, code, percent, months):
        listing = payments._request('GET', '/v1/promotion_codes?limit=100')
        for pc in listing.get('data', []):
            if (pc.get('code') or '').upper() == code.upper():
                self._line(OK, f'cupon {code}', f'ya existe [{pc["id"]}]')
                return
        if self.dry:
            self._line(WARN, f'cupon {code}', f'se crearia {percent}% x {months or 1} mes(es)')
            return

        duration = 'repeating' if months else 'once'
        # Reuse a matching coupon instead of minting one per run: the promotion
        # code can fail after the coupon is created, and a retry would otherwise
        # leave a trail of identical orphan coupons behind.
        coupon = next(
            (c for c in payments._request('GET', '/v1/coupons?limit=100').get('data', [])
             if c.get('valid')
             and (c.get('name') or '') == code.title()
             and c.get('percent_off') == float(percent)
             and c.get('duration') == duration
             and (not months or c.get('duration_in_months') == months)),
            None)
        if coupon is None:
            data = {'percent_off': str(percent), 'name': code.title(), 'duration': duration}
            if months:
                data['duration_in_months'] = str(months)
            coupon = payments._request('POST', '/v1/coupons', data)

        # Stripe renamed this field: `coupon` up to the 2025 versions, a typed
        # `promotion` object from 2026-07-29 (dahlia) on. Which one an account
        # gets depends on the version it is pinned to, so try the new shape and
        # fall back rather than guessing from the key.
        try:
            pc = payments._request('POST', '/v1/promotion_codes', {
                'code': code.upper(),
                'promotion[type]': 'coupon',
                'promotion[coupon]': coupon['id'],
            })
        except payments.StripeError as e:
            if 'promotion' not in str(e).lower():
                raise
            pc = payments._request('POST', '/v1/promotion_codes',
                                   {'coupon': coupon['id'], 'code': code.upper()})
        self._line(OK, f'cupon {code}', f'{percent}% creado [{pc["id"]}]')

    # -- entry point -------------------------------------------------------
    def handle(self, *args, **opts):
        self.dry = opts['dry_run']
        key = getattr(settings, 'STRIPE_SECRET_KEY', '')
        if not key:
            raise CommandError('Falta STRIPE_SECRET_KEY en el .env.')
        live = key.startswith('sk_live_')
        mode = 'LIVE (dinero real)' if live else 'TEST'
        if live and not self.dry and not opts['yes']:
            raise CommandError('Llave live detectada. Repite con --yes si es lo que quieres.')
        self.stdout.write('\nStripe: modo ' + mode
                          + ('  [DRY-RUN, no escribe nada]' if self.dry else ''))
        payments.account_ping()
        self.stdout.write('')

        env = {}
        for plan_id in PAID_PLANS:
            plan = PLAN_BY_ID[plan_id]
            up = plan_id.upper()
            prod = self._product(f'dominio_{plan_id}', f'DOMINIO {plan["name"]}',
                                 f'Agente de IA para tu negocio - plan {plan["name"]}')
            env[f'STRIPE_PRICE_{up}_MONTHLY'] = self._price(
                prod['id'], f'{plan_id}-monthly', plan['monthly'], 'month',
                f'{plan["name"]} mensual')
            env[f'STRIPE_PRICE_{up}_ANNUAL'] = self._price(
                prod['id'], f'{plan_id}-annual', plan['annual'], 'year',
                f'{plan["name"]} anual')

            setup = self._product(
                f'dominio_{plan_id}_setup',
                f'Instalacion y configuracion - {plan["name"]}',
                'Cargo unico por instalacion, configuracion y entrenamiento inicial del agente')
            env[f'STRIPE_PRICE_{up}_SETUP'] = self._price(
                setup['id'], f'{plan_id}-setup', plan['setup'], None,
                f'{plan["name"]} instalacion')
            self.stdout.write('')

        tax_rate_id = ''
        if opts['tax_rate']:
            tax_rate_id = self._tax_rate(opts['tax_rate'], opts['tax_name'],
                                         opts['tax_country'])
        secret = self._webhook(opts['webhook_url']) if opts['webhook_url'] else ''
        if opts['coupon']:
            self._coupon(opts['coupon'], opts['coupon_percent'], opts['coupon_months'])

        self.stdout.write('\n--- pega esto en el .env del servidor ---')
        if secret:
            self.stdout.write(f'STRIPE_WEBHOOK_SECRET={secret}')
        if tax_rate_id:
            self.stdout.write(f'STRIPE_TAX_RATE_ID={tax_rate_id}')
        for k, v in env.items():
            self.stdout.write(f'{k}={v}')
        self.stdout.write('---')
        if secret:
            self.stdout.write(self.style.WARNING(
                'El webhook secret de arriba NO se vuelve a mostrar. Guardalo ahora.'))
        self.stdout.write(
            '\nFalta a mano en el dashboard: branding, correos y customer portal '
            '(secciones 1, 5 y 6 de docs/lanzamiento/01-stripe.md).\n'
            'Verifica al final con: python manage.py preflight\n')
