"""
M-01 — Stripe integration, dependency-free.

Uses Stripe's plain HTTPS API (form-encoded) via urllib so no SDK is needed.
Everything degrades gracefully: with no STRIPE_SECRET_KEY configured the
signup keeps its current email-coordination flow, same pattern as the agent
and ANTHROPIC_API_KEY.

Card data NEVER touches this server: checkout happens on Stripe's hosted page
and we only receive webhooks about the result.
"""
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

API_BASE = 'https://api.stripe.com'
TIMEOUT = 15


class StripeError(Exception):
    pass


def stripe_enabled():
    return bool(getattr(settings, 'STRIPE_SECRET_KEY', ''))


def price_id_for(plan, period):
    """Stripe Price id for a plan/period, from settings (env-driven). Returns
    '' when not configured — that plan then falls back to the email flow."""
    prices = getattr(settings, 'STRIPE_PRICES', {})
    return prices.get(f'{plan}:{period}', '')


def setup_price_id_for(plan):
    """One-time Stripe Price id for a plan's setup fee ('' = no setup fee)."""
    prices = getattr(settings, 'STRIPE_PRICES', {})
    return prices.get(f'{plan}:setup', '')


def _request(method, path, data=None):
    """Authenticated form-encoded request to the Stripe API."""
    key = getattr(settings, 'STRIPE_SECRET_KEY', '')
    if not key:
        raise StripeError('Stripe is not configured.')
    url = API_BASE + path
    body = urllib.parse.urlencode(data or {}).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header('Authorization', f'Bearer {key}')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = json.loads(e.read().decode('utf-8')).get('error', {}).get('message', '')
        except Exception:
            pass
        logger.error('Stripe API %s %s failed (%s): %s', method, path, e.code, detail)
        raise StripeError(detail or f'Stripe API error {e.code}')
    except Exception as e:
        logger.exception('Stripe API %s %s failed', method, path)
        raise StripeError(str(e))


def tax_percent():
    """IVU percentage as a float, or None when no tax rate is configured.

    Cached for a day: the rate never changes in place (Stripe tax rates are
    immutable), and the welcome page must not spend a 15 s API call — with a
    Stripe outage it would just render the receipt without the tax line.
    """
    rate_id = getattr(settings, 'STRIPE_TAX_RATE_ID', '')
    if not rate_id or not stripe_enabled():
        return None
    from django.core.cache import cache
    key = f'taxpct:{rate_id}'
    cached = cache.get(key)
    if cached is not None:
        return cached or None          # '' is the cached "lookup failed"
    try:
        pct = float(_request('GET', '/v1/tax_rates/' + urllib.parse.quote(rate_id, safe=''))
                    .get('percentage') or 0) or None
    except (StripeError, TypeError, ValueError):
        logger.exception('Could not read tax rate %s', rate_id)
        cache.set(key, '', 60)
        return None
    cache.set(key, pct or '', 60 * 60 * 24)
    return pct


def retrieve_price(price_id):
    """Fetch one Price. Used by `manage.py preflight` to prove the amount Stripe
    will charge matches the amount the site advertises."""
    return _request('GET', '/v1/prices/' + urllib.parse.quote(str(price_id), safe=''))


def account_ping():
    """Cheapest authenticated call that proves the secret key works."""
    return _request('GET', '/v1/prices?limit=1')


def create_checkout_session(plan, period, email, success_url, cancel_url,
                            setup_price='', metadata=None):
    """Start a hosted subscription checkout. Returns the session dict
    (session['url'] is where to redirect the prospect).

    `setup_price` (optional) is a ONE-TIME Price id charged in the same session
    as the first period — Stripe allows one-time line items in subscription
    mode. `metadata` (optional) rides on the session AND the subscription so the
    webhook can provision the tenant (company, website, lead id...).
    """
    price = price_id_for(plan, period)
    if not price:
        raise StripeError(f'No Stripe price configured for {plan}/{period}.')
    data = {
        'mode': 'subscription',
        'line_items[0][price]': price,
        'line_items[0][quantity]': '1',
        'success_url': success_url,
        'cancel_url': cancel_url,
        'allow_promotion_codes': 'true',
    }
    if setup_price:
        data['line_items[1][price]'] = setup_price
        data['line_items[1][quantity]'] = '1'
    # IVU is charged ON TOP of the advertised price, so it rides on every line
    # item — the subscription AND the one-time setup fee. Unset = no tax line.
    tax_rate = getattr(settings, 'STRIPE_TAX_RATE_ID', '')
    if tax_rate:
        data['line_items[0][tax_rates][0]'] = tax_rate
        if setup_price:
            data['line_items[1][tax_rates][0]'] = tax_rate
    meta = {'plan': plan, 'period': period}
    for k, v in (metadata or {}).items():
        if v is None or v == '':
            continue
        meta[str(k)[:40]] = str(v)[:500]  # Stripe limits: 40-char keys, 500-char values
    for k, v in meta.items():
        data[f'metadata[{k}]'] = v
        data[f'subscription_data[metadata][{k}]'] = v
    if email:
        data['customer_email'] = email
    return _request('POST', '/v1/checkout/sessions', data)


def retrieve_checkout_session(session_id):
    """Fetch a Checkout Session by id (used by the post-payment welcome page to
    confirm `payment_status == 'paid'` server-side — the id in the URL is never
    trusted on its own)."""
    if not session_id or not session_id.startswith('cs_'):
        raise StripeError('Invalid checkout session id.')
    return _request('GET', f'/v1/checkout/sessions/{urllib.parse.quote(session_id)}')


def create_portal_session(customer_id, return_url):
    """Stripe Customer Portal: the client manages payment method, invoices,
    plan changes and cancellation on Stripe's hosted page — zero billing UI to
    maintain here. Returns the session dict (session['url'])."""
    if not customer_id:
        raise StripeError('No Stripe customer for this client.')
    return _request('POST', '/v1/billing_portal/sessions',
                    {'customer': customer_id, 'return_url': return_url})


def retrieve(path):
    """GET any Stripe object, e.g. retrieve('/v1/subscriptions/sub_...')."""
    return _request('GET', path)


def recent_checkout_sessions(since_ts, limit=100):
    """Paid Checkout Sessions created since `since_ts` (unix seconds).

    This is the safety net under the webhook: if a delivery is lost, or the
    worker dies mid-provisioning, the money is already ours and Stripe will not
    tell us again. Listing what actually got paid is the only way to notice.
    """
    return _request('GET', f'/v1/checkout/sessions?limit={int(limit)}'
                           f'&created[gte]={int(since_ts)}')


def verify_webhook_signature(payload, sig_header, tolerance=300):
    """Verify a Stripe-Signature header (v1 scheme) against the raw payload.

    Returns True only when a v1 signature matches HMAC-SHA256(secret,
    '{t}.{payload}') and the timestamp is within `tolerance` seconds — the
    standard Stripe scheme, implemented per their docs.
    """
    secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '')
    if not secret or not sig_header:
        return False
    try:
        parts = dict(p.split('=', 1) for p in sig_header.split(',') if '=' in p)
        timestamp = int(parts.get('t', '0'))
    except (ValueError, TypeError):
        return False
    if abs(time.time() - timestamp) > tolerance:
        return False
    signed = f'{timestamp}.'.encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    # The header may carry several v1 signatures (during secret rotation).
    candidates = [v for k, v in
                  (p.split('=', 1) for p in sig_header.split(',') if '=' in p)
                  if k.strip() == 'v1']
    return any(hmac.compare_digest(expected, c.strip()) for c in candidates)
