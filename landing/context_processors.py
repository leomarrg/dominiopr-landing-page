from django.conf import settings
from django.utils.functional import SimpleLazyObject

from . import human


def site_globals(request):
    """Expose a few settings to every template (e.g. the GA measurement id)."""
    return {
        'GA_MEASUREMENT_ID': getattr(settings, 'GA_MEASUREMENT_ID', ''),
        # Only render the chat widget when an API key is actually configured.
        'AI_CHAT_ENABLED': bool(getattr(settings, 'ANTHROPIC_API_KEY', '')),
        # Canonical/OG URLs are built from the public domain, never the Host header.
        'SITE_URL': getattr(settings, 'SITE_URL', 'https://dominiopr.com').rstrip('/'),
        # Prices are quoted without tax. Only disclose "+ IVU" when a tax rate
        # is actually configured, or the page would promise a charge that
        # Checkout never makes.
        'TAX_ON_TOP': bool(getattr(settings, 'STRIPE_TAX_RATE_ID', '')),
        # hCaptcha: templates render the checkbox only when both keys exist.
        'HCAPTCHA_SITEKEY': getattr(settings, 'HCAPTCHA_SITEKEY', ''),
        'HUMAN_CHECK_ENABLED': human.enabled(),
    }


def dash_roles(request):
    """M-17: role flags for the dashboard nav. Lazy so public pages never pay
    the membership query."""
    def _is_org_admin():
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff:
            return True
        return user.memberships.filter(role='org_admin').exists()

    def _has_bookings():
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff:
            return True
        return user.memberships.filter(client__enable_bookings=True).exclude(
            role='employee').exists()

    def _is_employee_only():
        """Phase E: every membership is 'employee' (and not staff): the nav
        shows only the assistant."""
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated or user.is_staff:
            return False
        roles = set(user.memberships.values_list('role', flat=True))
        return bool(roles) and roles == {'employee'}

    def _has_internal():
        """Phase E: some membership's plan includes the internal assistant
        (the page itself re-checks the entitlement)."""
        from . import entitlements  # lazy: this module loads before the apps
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff:
            return True
        clients = [m.client for m in user.memberships.select_related('client__subscription')]
        return any('internal' in entitlements.entitlements(c).get('channels', ())
                   for c in clients)

    return {
        'IS_ORG_ADMIN': SimpleLazyObject(_is_org_admin),
        'HAS_BOOKINGS': SimpleLazyObject(_has_bookings),
        'IS_EMPLOYEE_ONLY': SimpleLazyObject(_is_employee_only),
        'HAS_INTERNAL': SimpleLazyObject(_has_internal),
    }
