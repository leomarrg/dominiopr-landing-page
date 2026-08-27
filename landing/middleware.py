"""Request-level guards for the branded dashboard."""
from django.contrib import messages
from django.shortcuts import redirect

from .models import Membership

DASHBOARD_PREFIX = '/dashboard/'

# Routes a gated user may still reach: getting in/out and fixing the password.
EXEMPT_URL_NAMES = frozenset({
    'login', 'logout', 'password_change',
    'password_reset', 'password_reset_done', 'password_reset_confirm',
    'password_reset_complete',
})

GATE_MESSAGE = 'Crea una nueva contraseña para terminar de configurar tu cuenta.'


class TempPasswordGateMiddleware:
    """Auto-provisioned accounts sign in with a temporary password. Until it
    is replaced, every /dashboard/ page (leads, conversations, install,
    billing, exports, ...) redirects to the password-change form.

    Implemented in `process_view` so the resolved URL name is available. The
    cost is one `exists()` query, and only for authenticated non-staff users
    on a non-exempt dashboard path.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        if not request.path.startswith(DASHBOARD_PREFIX):
            return None
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated or user.is_staff:
            return None
        match = getattr(request, 'resolver_match', None)
        if match is not None and match.url_name in EXEMPT_URL_NAMES:
            return None
        if not Membership.objects.filter(user=user, must_change_password=True).exists():
            return None
        messages.info(request, GATE_MESSAGE)
        return redirect('password_change')


class ContentSecurityPolicyMiddleware:
    """Content-Security-Policy for our own pages.

    Why it earns its place even with 'unsafe-inline': the site has a lot of
    inline <script> blocks, so we cannot lock down script execution without
    rewriting all of them with nonces. But the directives that matter against
    the realistic attacks here do NOT depend on that:

    * `default-src 'self'` + `script-src` allowlist — an injected
      `<script src="//evil.tld/x.js">` never loads, which is how stolen data
      actually leaves a page.
    * `connect-src 'self'` — exfiltration via fetch/XHR/beacon to an attacker's
      server is blocked, which is the payload of most XSS.
    * `object-src 'none'` and `base-uri 'self'` — kill Flash/plugin vectors and
      <base> hijacking, neither of which we use.
    * `frame-ancestors 'none'` — clickjacking, and unlike X-Frame-Options it is
      honoured by every modern browser.
    * `form-action` — an injected form cannot POST credentials off-site.

    Applied here rather than in nginx so it also holds in development and is
    covered by the test suite. `/widget.js` runs on OUR customers' sites, where
    their CSP applies, not ours — this header does not affect them.
    """

    # Google Tag Manager serves the GA snippet; GA beacons go to two hosts.
    GA_SCRIPT = 'https://www.googletagmanager.com'
    GA_CONNECT = ('https://www.google-analytics.com '
                  'https://region1.google-analytics.com '
                  'https://stats.g.doubleclick.net')

    POLICY = (
        "default-src 'self'; "
        f"script-src 'self' 'unsafe-inline' {GA_SCRIPT}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://www.google-analytics.com "
        "https://www.googletagmanager.com; "
        "font-src 'self'; "
        f"connect-src 'self' {GA_CONNECT}; "
        "form-action 'self' https://checkout.stripe.com https://billing.stripe.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        # Never send it with widget.js: that file is executed on a customer's
        # own site, and a CSP on the script response would be meaningless there
        # while risking confusion when debugging their page.
        if request.path == '/widget.js':
            return response
        response.setdefault('Content-Security-Policy', self.POLICY)
        return response
