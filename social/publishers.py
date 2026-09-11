"""M-20 — Instagram / Facebook / TikTok publishing, dependency-free.

Plain HTTPS over urllib, same pattern as `landing.payments`: no SDK to keep
patched, nothing new in requirements.txt. A platform with no credentials
configured is reported as disabled and skipped, so the queue degrades to
"nothing publishes" rather than half-publishing.

Every platform here is TWO-PHASE, and that shape is the whole reason the model
carries `remote_ref`:

  Instagram  POST /{ig_user}/media -> container id, then poll it until
             FINISHED, then POST /{ig_user}/media_publish.
  Facebook   feed  = POST /{page}/photos (published=true) in one shot.
             story = POST /{page}/photos (published=false) -> photo id, then
                     POST /{page}/photo_stories.
  TikTok     POST /v2/post/publish/content/init/ -> publish_id, then poll
             /v2/post/publish/status/fetch/ until PUBLISH_COMPLETE.

So a publish can legitimately end "not finished yet". That is returned as
`(external_id='', remote_ref=<token>)` — the caller persists the token and the
next cron run resumes the SAME upload instead of creating a second copy of the
image on the platform.

Errors are split into retryable and not, because retrying the wrong one is how
you either give up on a blip or hammer an API with a request it will never
accept: transport failures, 429 and 5xx retry; a rejected caption, an
unreachable media_url or a revoked token do not.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

GRAPH_BASE = 'https://graph.facebook.com'
TIKTOK_BASE = 'https://open.tiktokapis.com'
TIMEOUT = 30
# Instagram containers for a single image finish in a second or two. Poll a few
# times inside this run, then hand the token back and let cron resume: holding
# the command open for minutes just to watch a spinner is worse.
CONTAINER_POLLS = 5
CONTAINER_WAIT = 2


class PublishError(Exception):
    """A publish attempt failed. `retryable` decides whether publish_due backs
    off and tries again or marks the post failed on the spot."""

    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def _graph(path):
    version = getattr(settings, 'SOCIAL_GRAPH_VERSION', 'v21.0')
    return f'{GRAPH_BASE}/{version}/{path.lstrip("/")}'


def enabled_for(platform):
    """Are this platform's credentials actually configured?"""
    token = getattr(settings, 'SOCIAL_META_ACCESS_TOKEN', '')
    if platform == 'instagram':
        return bool(token and getattr(settings, 'SOCIAL_IG_USER_ID', ''))
    if platform == 'facebook':
        return bool(token and getattr(settings, 'SOCIAL_FB_PAGE_ID', ''))
    if platform == 'tiktok':
        return bool(getattr(settings, 'SOCIAL_TIKTOK_ACCESS_TOKEN', ''))
    return False


# ---------------------------------------------------------------- transport

def _call(url, form=None, body=None, headers=None):
    """One HTTP call. `form` = form-encoded POST (Meta), `body` = JSON POST
    (TikTok), neither = GET. Raises PublishError, already classified."""
    data = None
    hdrs = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode('utf-8')
        hdrs['Content-Type'] = 'application/json; charset=UTF-8'
    req = urllib.request.Request(url, data=data, method='POST' if data else 'GET')
    for key, value in hdrs.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        raw = ''
        try:
            raw = e.read().decode('utf-8')
        except Exception:
            pass
        message, retryable = _classify(e.code, raw)
        logger.error('Social API %s failed (%s): %s', _scrub(url), e.code, message)
        raise PublishError(message, retryable=retryable)
    except Exception as e:
        # Timeout, DNS, TLS, connection reset: always worth another run.
        logger.warning('Social API %s transport error: %s', _scrub(url), e)
        raise PublishError(f'error de red: {e}', retryable=True)


def _classify(code, raw):
    """Turn an HTTP error into (message, retryable).

    Meta flags its own transient failures with `is_transient`; trust that over
    the status code, because Meta answers 400 for real outages too. Everything
    else follows the usual rule: 429 and 5xx retry, other 4xx are our own bad
    request and will fail identically forever.
    """
    detail = ''
    transient = None
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}
    if isinstance(payload, dict) and isinstance(payload.get('error'), dict):
        err = payload['error']
        detail = err.get('message') or err.get('error_user_msg') or ''
        err_code = str(err.get('code') or '')
        if err_code:
            detail = f'[{err_code}] {detail}'
        if 'is_transient' in err:
            transient = bool(err['is_transient'])
    elif isinstance(payload, dict) and payload.get('error'):
        detail = str(payload['error'])          # TikTok returns a string/obj
    if not detail:
        detail = (raw or '')[:200]
    retryable = transient if transient is not None else (code == 429 or code >= 500)
    return (detail or f'HTTP {code}'), retryable


def _scrub(url):
    """Never let an access token reach the log file."""
    return url.split('?')[0]


def _meta_token():
    token = getattr(settings, 'SOCIAL_META_ACCESS_TOKEN', '')
    if not token:
        raise PublishError('SOCIAL_META_ACCESS_TOKEN no esta configurado.', retryable=False)
    return token


# ---------------------------------------------------------------- platforms

def publish(post):
    """Publish one Post. Returns `(external_id, remote_ref)`.

    An empty external_id means the platform accepted the media but has not
    finished publishing it: persist remote_ref and call again later.
    """
    if not enabled_for(post.platform):
        raise PublishError(f'{post.platform} no tiene credenciales configuradas.',
                           retryable=False)
    if (post.platform, post.kind) in post.UNSUPPORTED:
        raise PublishError(f'{post.platform} no soporta {post.kind}.', retryable=False)
    if not post.media_url.lower().startswith('https://'):
        # Meta and TikTok both refuse to fetch anything else, and the refusal
        # comes back as a generic error much later. Fail here, clearly.
        raise PublishError('media_url tiene que ser HTTPS publica.', retryable=False)

    handler = {'instagram': _instagram, 'facebook': _facebook, 'tiktok': _tiktok}
    return handler[post.platform](post)


def _instagram(post):
    token = _meta_token()
    ig_user = settings.SOCIAL_IG_USER_ID

    container = post.remote_ref
    if not container:
        form = {'image_url': post.media_url, 'access_token': token}
        if post.kind == 'story':
            form['media_type'] = 'STORIES'      # stories carry no caption
        elif post.caption:
            form['caption'] = post.caption
        container = str(_call(_graph(f'{ig_user}/media'), form=form).get('id') or '')
        if not container:
            raise PublishError('Instagram no devolvio container id.', retryable=True)

    # Wait for ingestion: an unfinished container cannot be published, and
    # publishing it anyway returns a confusing "media not ready" error.
    ready = False
    for attempt in range(CONTAINER_POLLS):
        status = _call(_graph(container) + '?' + urllib.parse.urlencode(
            {'fields': 'status_code,status', 'access_token': token}))
        code = status.get('status_code')
        if code == 'FINISHED':
            ready = True
            break
        if code == 'ERROR':
            raise PublishError(
                f'Instagram rechazo el media: {status.get("status", "")}'[:280],
                retryable=False)
        if code == 'EXPIRED':
            raise PublishError('El container de Instagram expiro.', retryable=True)
        if attempt < CONTAINER_POLLS - 1:
            time.sleep(CONTAINER_WAIT)
    if not ready:
        return '', container                    # still IN_PROGRESS

    published = _call(_graph(f'{ig_user}/media_publish'),
                      form={'creation_id': container, 'access_token': token})
    external_id = str(published.get('id') or '')
    if not external_id:
        raise PublishError('Instagram no devolvio id del post publicado.', retryable=True)
    return external_id, container


def _facebook(post):
    token = _meta_token()
    page = settings.SOCIAL_FB_PAGE_ID

    if post.kind == 'feed':
        form = {'url': post.media_url, 'published': 'true', 'access_token': token}
        if post.caption:
            form['caption'] = post.caption
        result = _call(_graph(f'{page}/photos'), form=form)
        external_id = str(result.get('post_id') or result.get('id') or '')
        if not external_id:
            raise PublishError('Facebook no devolvio id del post.', retryable=True)
        return external_id, ''

    # Story: the photo must exist unpublished first, then be turned into a
    # story. Keeping the photo id in remote_ref means a failure in the second
    # call does not upload the image a second time.
    photo_id = post.remote_ref
    if not photo_id:
        uploaded = _call(_graph(f'{page}/photos'), form={
            'url': post.media_url, 'published': 'false', 'access_token': token})
        photo_id = str(uploaded.get('id') or '')
        if not photo_id:
            raise PublishError('Facebook no devolvio id de la foto.', retryable=True)
    story = _call(_graph(f'{page}/photo_stories'),
                  form={'photo_id': photo_id, 'access_token': token})
    external_id = str(story.get('post_id') or story.get('id') or photo_id)
    return external_id, photo_id


def _tiktok(post):
    token = settings.SOCIAL_TIKTOK_ACCESS_TOKEN
    headers = {'Authorization': f'Bearer {token}'}

    publish_id = post.remote_ref
    if not publish_id:
        body = {
            'post_info': {
                'title': (post.caption or '')[:90],
                'description': post.caption or '',
                'privacy_level': getattr(settings, 'SOCIAL_TIKTOK_PRIVACY_LEVEL',
                                         'PUBLIC_TO_EVERYONE'),
            },
            'source_info': {
                'source': 'PULL_FROM_URL',
                'photo_cover_index': 0,
                'photo_images': [post.media_url],
            },
            'post_mode': 'DIRECT_POST',
            'media_type': 'PHOTO',
        }
        data = (_call(f'{TIKTOK_BASE}/v2/post/publish/content/init/',
                      body=body, headers=headers).get('data') or {})
        publish_id = str(data.get('publish_id') or '')
        if not publish_id:
            raise PublishError('TikTok no devolvio publish_id.', retryable=True)

    status = (_call(f'{TIKTOK_BASE}/v2/post/publish/status/fetch/',
                    body={'publish_id': publish_id}, headers=headers).get('data') or {})
    state = status.get('status', '')
    if state == 'PUBLISH_COMPLETE':
        ids = status.get('publicaly_available_post_id') or status.get('public_post_id') or []
        return (str(ids[0]) if ids else publish_id), publish_id
    if state == 'FAILED':
        reason = status.get('fail_reason', '') or 'sin razon'
        # A bad URL or an unaudited privacy level fails the same way on retry.
        raise PublishError(f'TikTok fallo: {reason}'[:280], retryable=False)
    return '', publish_id                       # PROCESSING_* — resume next run
