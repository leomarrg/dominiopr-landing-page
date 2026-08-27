"""Prove the production configuration is real before a paying stranger hits it.

    python manage.py preflight            # everything
    python manage.py preflight --stripe   # only the money path
    python manage.py preflight --email    # only deliverability

Why this exists: almost every integration here degrades *silently* by design, so
a misconfiguration does not raise — it quietly serves a worse flow. If a Stripe
Price id is missing, `payments.price_id_for()` returns '' and checkout falls back
to the email flow; the prospect never sees an error and you never learn. Worse,
nothing anywhere checks that the amount Stripe charges equals the amount the site
advertises, so a Price id pasted into the wrong variable bills the wrong number.

This command turns every one of those silent failures into a loud line, using
read-only API calls. It never writes to Stripe and never sends mail to a customer.
"""
import smtplib

from django.conf import settings
from django.core.management.base import BaseCommand

from landing import payments
from landing.views import PLANS

OK, WARN, FAIL = 'ok', 'warn', 'fail'
MARK = {OK: '[ ok ]', WARN: '[warn]', FAIL: '[FAIL]'}


class Command(BaseCommand):
    help = 'Verify Stripe, email, AI and Django settings are really configured.'

    def add_arguments(self, parser):
        parser.add_argument('--stripe', action='store_true', help='Only the Stripe/payment checks.')
        parser.add_argument('--email', action='store_true', help='Only the email checks.')
        parser.add_argument('--django', action='store_true', help='Only the Django/security checks.')
        parser.add_argument('--ai', action='store_true', help='Only the AI agent checks.')

    # ---- output helpers -------------------------------------------------

    def _line(self, status, label, detail=''):
        self.results.append(status)
        style = {OK: self.style.SUCCESS, WARN: self.style.WARNING, FAIL: self.style.ERROR}[status]
        self.stdout.write(f'{style(MARK[status])} {label}' + (f' — {detail}' if detail else ''))

    def _section(self, title):
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(title))

    # ---- main -----------------------------------------------------------

    def handle(self, *args, **opts):
        self.results = []
        chosen = {k for k in ('stripe', 'email', 'django', 'ai') if opts.get(k)}
        run_all = not chosen

        if run_all or 'django' in chosen:
            self.check_django()
        if run_all or 'stripe' in chosen:
            self.check_stripe()
        if run_all or 'email' in chosen:
            self.check_email()
        if run_all or 'ai' in chosen:
            self.check_ai()

        fails = self.results.count(FAIL)
        warns = self.results.count(WARN)
        self.stdout.write('')
        if fails:
            self.stdout.write(self.style.ERROR(
                f'{fails} blocking problem(s), {warns} warning(s). '
                'Do NOT point real traffic at this until the FAIL lines are green.'))
        elif warns:
            self.stdout.write(self.style.WARNING(
                f'No blocking problems, {warns} warning(s) worth a look before launch.'))
        else:
            self.stdout.write(self.style.SUCCESS('All checks passed — safe to take real payments.'))

    # ---- Django ---------------------------------------------------------

    def check_django(self):
        self._section('Django / security')

        if settings.DEBUG:
            self._line(FAIL, 'DEBUG', 'is True — expected on your laptop, must be False on the server')
        else:
            self._line(OK, 'DEBUG', 'False')

        key = getattr(settings, 'SECRET_KEY', '')
        if len(key) < 50 or key.startswith('django-insecure-'):
            self._line(FAIL, 'SECRET_KEY', 'too short or still the generated default')
        else:
            self._line(OK, 'SECRET_KEY', f'{len(key)} chars')

        hosts = [h for h in getattr(settings, 'ALLOWED_HOSTS', []) if h not in ('localhost', '127.0.0.1')]
        if not hosts:
            self._line(FAIL, 'ALLOWED_HOSTS', 'no real domain configured')
        elif '*' in hosts:
            self._line(FAIL, 'ALLOWED_HOSTS', 'contains "*" — accepts any Host header')
        else:
            self._line(OK, 'ALLOWED_HOSTS', ', '.join(hosts))

        if not getattr(settings, 'CSRF_TRUSTED_ORIGINS', []):
            self._line(WARN, 'CSRF_TRUSTED_ORIGINS', 'empty — POSTs over HTTPS may be rejected')
        else:
            self._line(OK, 'CSRF_TRUSTED_ORIGINS', f'{len(settings.CSRF_TRUSTED_ORIGINS)} origin(s)')

        for name in ('SESSION_COOKIE_SECURE', 'CSRF_COOKIE_SECURE'):
            if getattr(settings, name, False):
                self._line(OK, name, 'True')
            else:
                self._line(FAIL, name, 'False — the cookie can travel over plain HTTP')

        hsts = getattr(settings, 'SECURE_HSTS_SECONDS', 0)
        if not hsts:
            self._line(WARN, 'SECURE_HSTS_SECONDS', '0 — no HSTS')
        elif hsts < 86400:
            self._line(WARN, 'SECURE_HSTS_SECONDS', f'{hsts}s — fine while testing, raise to 31536000 once stable')
        else:
            self._line(OK, 'SECURE_HSTS_SECONDS', f'{hsts}s')

    # ---- Stripe ---------------------------------------------------------

    def check_stripe(self):
        self._section('Stripe / the money path')

        key = getattr(settings, 'STRIPE_SECRET_KEY', '')
        if not key:
            self._line(FAIL, 'STRIPE_SECRET_KEY', 'missing — checkout silently falls back to the email flow')
            return
        live = key.startswith('sk_live_')
        self._line(OK if live else WARN, 'STRIPE_SECRET_KEY',
                   'LIVE mode' if live else 'TEST mode — real cards will not be charged')

        try:
            payments.account_ping()
            self._line(OK, 'Stripe API', 'key accepted')
        except payments.StripeError as e:
            self._line(FAIL, 'Stripe API', f'key rejected: {e}')
            return

        if not getattr(settings, 'STRIPE_WEBHOOK_SECRET', ''):
            self._line(FAIL, 'STRIPE_WEBHOOK_SECRET',
                       'missing — every webhook is rejected, so nobody who pays gets provisioned')
        else:
            self._line(OK, 'STRIPE_WEBHOOK_SECRET', 'set')

        # The core check: what Stripe will charge vs what the site advertises.
        for plan in PLANS:
            if plan['monthly'] is None:      # "Custom" is quote-only, no Price id
                continue
            for period, expected, kind in (
                ('monthly', plan['monthly'], 'recurring-month'),
                ('annual', plan['annual'], 'recurring-year'),
                ('setup', plan['setup'], 'one_time'),
            ):
                self._check_price(plan, period, expected, kind)

        self._check_tax()

    def _check_tax(self):
        """The site quotes prices without tax, so a missing tax rate means every
        invoice goes out untaxed — silently, and Stripe cannot bill it later."""
        rate_id = getattr(settings, 'STRIPE_TAX_RATE_ID', '')
        if not rate_id:
            self._line(WARN, 'STRIPE_TAX_RATE_ID',
                       'not set — Checkout charges the price with NO tax line')
            return
        try:
            rate = payments._request('GET', '/v1/tax_rates/' + rate_id)
        except payments.StripeError as e:
            self._line(FAIL, 'STRIPE_TAX_RATE_ID', f'{rate_id} could not be read: {e}')
            return
        problems = []
        if not rate.get('active'):
            problems.append('the rate is archived in Stripe')
        if rate.get('inclusive'):
            problems.append('the rate is INCLUSIVE — it would be carved out of '
                            'the price instead of added on top')
        if problems:
            self._line(FAIL, 'tax rate', '; '.join(problems))
        else:
            self._line(OK, 'tax rate',
                       f"{rate.get('display_name')} {rate.get('percentage')}% "
                       f"added on top [{rate_id}]")

    def _check_price(self, plan, period, expected_usd, kind):
        label = f"{plan['name']} {period}"
        price_id = (payments.setup_price_id_for(plan['id']) if period == 'setup'
                    else payments.price_id_for(plan['id'], period))

        if not price_id:
            if period == 'setup':
                self._line(WARN, label, 'no Price id — no setup fee will be charged for this plan')
            else:
                self._line(FAIL, label,
                           f"no Price id — /get-started/ advertises ${expected_usd} but checkout "
                           f"falls back to the email flow")
            return

        try:
            price = payments.retrieve_price(price_id)
        except payments.StripeError as e:
            self._line(FAIL, label, f'{price_id} could not be read: {e}')
            return

        problems = []
        if not price.get('active', False):
            problems.append('price is archived/inactive in Stripe')

        currency = (price.get('currency') or '').lower()
        if currency != 'usd':
            problems.append(f'currency is {currency.upper()}, expected USD')

        amount = price.get('unit_amount')
        if amount is None:
            problems.append('has no fixed unit_amount (tiered or metered pricing?)')
        elif amount != expected_usd * 100:
            problems.append(
                f'Stripe charges ${amount / 100:,.2f} but the site advertises ${expected_usd:,}')

        recurring = price.get('recurring') or {}
        interval = recurring.get('interval')
        if kind == 'one_time' and interval:
            problems.append(f'is recurring ({interval}) but the setup fee must be one-time')
        elif kind == 'recurring-month' and interval != 'month':
            problems.append(f'interval is {interval or "one-time"}, expected month')
        elif kind == 'recurring-year' and interval != 'year':
            problems.append(f'interval is {interval or "one-time"}, expected year')

        if problems:
            self._line(FAIL, label, '; '.join(problems) + f' [{price_id}]')
        else:
            self._line(OK, label, f'${expected_usd:,} {kind} [{price_id}]')

    # ---- Email ----------------------------------------------------------

    def check_email(self):
        self._section('Email')

        backend = getattr(settings, 'EMAIL_BACKEND', '')
        if 'console' in backend or 'locmem' in backend:
            self._line(FAIL, 'EMAIL_BACKEND', f'{backend} — nothing actually reaches a customer')
            return
        self._line(OK, 'EMAIL_BACKEND', backend.rsplit('.', 1)[-1])

        sender = getattr(settings, 'DEFAULT_FROM_EMAIL', '')
        if not sender or 'example.com' in sender or 'webmaster@' in sender:
            self._line(FAIL, 'DEFAULT_FROM_EMAIL', f'{sender or "unset"} — set your own domain')
        else:
            self._line(OK, 'DEFAULT_FROM_EMAIL', sender)

        notify = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
        self._line(OK if notify else FAIL, 'CONTACT_NOTIFY_EMAIL',
                   notify or 'unset — new leads would notify nobody')

        host = getattr(settings, 'EMAIL_HOST', '')
        password = getattr(settings, 'EMAIL_HOST_PASSWORD', '')
        if not host or not password:
            self._line(FAIL, 'SMTP credentials', 'EMAIL_HOST or EMAIL_HOST_PASSWORD missing')
            return

        # Connect and authenticate, but send nothing.
        port = getattr(settings, 'EMAIL_PORT', 587)
        try:
            with smtplib.SMTP(host, port, timeout=getattr(settings, 'EMAIL_TIMEOUT', 10) or 10) as smtp:
                smtp.ehlo()
                if getattr(settings, 'EMAIL_USE_TLS', False):
                    smtp.starttls()
                    smtp.ehlo()
                smtp.login(getattr(settings, 'EMAIL_HOST_USER', ''), password)
            self._line(OK, 'SMTP login', f'{host}:{port} accepted the credentials')
        except smtplib.SMTPAuthenticationError:
            self._line(FAIL, 'SMTP login', f'{host}:{port} rejected the credentials')
        except Exception as e:
            self._line(FAIL, 'SMTP login', f'{host}:{port} unreachable: {e}')

    # ---- AI -------------------------------------------------------------

    def check_ai(self):
        self._section('AI agent')

        key = getattr(settings, 'ANTHROPIC_API_KEY', '')
        if not key:
            self._line(FAIL, 'ANTHROPIC_API_KEY',
                       'missing — the chat widget answers with the canned fallback, not the agent')
            return
        self._line(OK, 'ANTHROPIC_API_KEY', f'set ({key[:7]}…)')

        try:
            import anthropic  # noqa: F401
        except ImportError:
            self._line(FAIL, 'anthropic package',
                       'not installed — add it to requirements.txt or the agent cannot run')
            return
        self._line(OK, 'anthropic package', 'installed')

        # Mirrors the default in landing/agent.py so a stale pin shows up here.
        model = getattr(settings, 'DOMINIO_AGENT_MODEL', 'claude-haiku-4-5')
        self._line(OK, 'model', model)
