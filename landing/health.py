"""Operational plumbing: the /healthz/ probe and the alert brake.

/healthz/ is what the uptime monitor and the deploy smoke test hit. It checks
the two things every page needs (the SQLite database and the DatabaseCache
table the rate limits and spend caps live in) and nothing else: no session,
no user, no tenant data, no external call. 200 {"ok": true} or 503
{"ok": false}; the body never says which part failed (that goes to the log).

alert() is how a recurring failure reaches the admins without flooding them:
every occurrence is a WARNING (journald), and at most one ERROR per `kind`
per hour (the 'landing' logger mails ADMINS at ERROR).

csrf_failure() is settings.CSRF_FAILURE_VIEW: the branded Spanish page
instead of Django's English debug-style one.
"""
import logging
import uuid

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)

HEALTH_CACHE_KEY = 'healthz:probe'
ALERT_WINDOW = 60 * 60


@csrf_exempt
@never_cache
@require_http_methods(['GET', 'HEAD'])
def healthz(request):
    ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception as exc:
        ok = False
        logger.warning('healthz: database check failed (%s)', exc.__class__.__name__)
    if ok:
        try:
            token = uuid.uuid4().hex
            cache.set(HEALTH_CACHE_KEY, token, 60)
            if cache.get(HEALTH_CACHE_KEY) != token:
                raise RuntimeError('cache read-back mismatch')
        except Exception as exc:
            ok = False
            logger.warning('healthz: cache check failed (%s)', exc.__class__.__name__)
    resp = JsonResponse({'ok': ok}, status=200 if ok else 503)
    # never_cache already sets no-cache/no-store/must-revalidate/private;
    # pin it so a proxy in front never answers for the app.
    resp['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


def csrf_failure(request, reason=''):
    """CSRF_FAILURE_VIEW. The reason is Django's fixed English string (no
    user data); it goes to the log, never to the page."""
    logger.warning('CSRF failure on %s: %s', request.path, reason)
    return render(request, '403_csrf.html', status=403)


def quiet(response):
    """Mark an EXPECTED 5xx (model down, a spend cap reached, a missing
    secret) as already logged, so django.request does not log it again at
    ERROR, which mails ADMINS on every single request. The caller has already
    logged it, through alert() when it is worth an email. Same flag Django
    sets itself (django.utils.log.log_response). Unexpected 500s are never
    passed here and keep their email."""
    response._has_been_logged = True
    return response


def alert(log, kind, message, *args, exc_info=False):
    """Log a recurring failure: WARNING every time, ERROR (= email to
    ADMINS) at most once per hour per `kind`. `log` is the caller's logger so
    the record keeps its module name. Returns True when the ERROR was sent.
    If the cache is down the ERROR goes out: better one email too many than
    an outage nobody hears about."""
    try:
        first = cache.add(f'alert:{kind}', 1, ALERT_WINDOW)
    except Exception:
        first = True
    if first:
        log.error(message, *args, exc_info=exc_info)
    else:
        log.warning(message, *args, exc_info=exc_info)
    return bool(first)
