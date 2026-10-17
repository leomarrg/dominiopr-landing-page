"""
Django settings for dominio_website project.
"""

import os
import sys
from pathlib import Path

# Optional: load .env file in local development
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


BASE_DIR = Path(__file__).resolve().parent.parent

# True under `manage.py test`. Used to switch off anything that would reach the
# network from the test suite (tests that need it turn it back on explicitly).
TESTING = sys.argv[1:2] == ['test']


# ============================================================
# CORE
# ============================================================

# DEBUG is OFF by default — you turn it ON deliberately in local dev (.env),
# never the reverse. A missing env var in production must fail safe.
DEBUG = os.environ.get('DJANGO_DEBUG', 'False').lower() in ('true', '1', 'yes')

# No insecure committed fallback in production: fail loudly if the key is missing.
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-dev-only-do-not-use-in-production'
    else:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured('DJANGO_SECRET_KEY must be set in production.')

# ALLOWED_HOSTS: leading dot matches the domain AND all subdomains.
# e.g. ".dominiopr.com" matches dominiopr.com, www.dominiopr.com,
# registratepr.dominiopr.com, etc.
_default_hosts = '.dominiopr.com,localhost,127.0.0.1' if not DEBUG else '*'
ALLOWED_HOSTS = [h.strip() for h in os.environ.get('DJANGO_ALLOWED_HOSTS', _default_hosts).split(',') if h.strip()]

# Default trusts only DOMINIO's own domains. Dev tunnels (e.g. ngrok) must be
# added via DJANGO_CSRF_TRUSTED_ORIGINS in the local .env — never trusted by
# default, so a forgotten env var in prod can't leave a wildcard tunnel trusted.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get(
        'DJANGO_CSRF_TRUSTED_ORIGINS',
        'https://dominiopr.com,https://www.dominiopr.com,https://*.dominiopr.com'
    ).split(',') if o.strip()
]

# Public base URL for links built OUTSIDE a request cycle (emails, jobs).
SITE_URL = os.environ.get('DJANGO_SITE_URL', 'https://dominiopr.com').rstrip('/')


# ============================================================
# APPS / MIDDLEWARE
# ============================================================

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sitemaps',
    'landing',
    'social',
]

# django-browser-reload only in DEBUG (dev convenience)
if DEBUG:
    try:
        import django_browser_reload  # noqa: F401
        INSTALLED_APPS.append('django_browser_reload')
    except ImportError:
        pass

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise serves static files efficiently in production
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    # Auto-provisioned accounts must replace their temp password before any
    # /dashboard/ page (needs auth + messages above).
    'landing.middleware.TempPasswordGateMiddleware',
    # Phase E: employee-only accounts get the internal assistant and nothing else.
    'landing.middleware.EmployeeScopeMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # CSP: blocks injected third-party scripts and off-site exfiltration.
    'landing.middleware.ContentSecurityPolicyMiddleware',
]

if DEBUG:
    try:
        import django_browser_reload  # noqa: F401
        MIDDLEWARE.append('django_browser_reload.middleware.BrowserReloadMiddleware')
    except ImportError:
        pass

ROOT_URLCONF = 'dominio_website.urls'

# Auth redirects for the custom dashboard (not Django admin).
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'login'

# ============================================================
# SECURITY — abuse / DoS protection for the public chat endpoint
# ============================================================
# Shared, persistent counter for rate limiting (survives worker restarts and
# is shared across gunicorn workers). SQLite-backed — no Redis needed.
# Run once after deploy: python manage.py createcachetable
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'dominio_cache',
        # DatabaseCache defaults to MAX_ENTRIES=300 and culls the
        # lexicographically SMALLEST keys. Our daily spend counters
        # ('chat:client:*', 'chat:global:*') sort near the front, so under a
        # burst of per-IP rate-limit keys ('rl:*') the caps would be evicted
        # and the API budget would silently reset to zero — exactly the
        # scenario they exist to guard against.
        'OPTIONS': {'MAX_ENTRIES': 100000, 'CULL_FREQUENCY': 4},
    },
    # Email-domain DNS verdicts. Kept OUT of the default cache: the domain is
    # attacker-chosen, so a flood of random domains would otherwise grow that
    # cache toward MAX_ENTRIES and the cull would evict the 'chat:*' spend
    # counters (they sort first). Per-process memory, small and bounded.
    'emaildns': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'dominio-emaildns',
        'OPTIONS': {'MAX_ENTRIES': 5000},
    },
}

# Reject oversized request bodies before they are parsed into memory (the chat
# endpoint never needs more than a few KB). Protects the 512MB box.
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024  # 1 MB hard cap on POST bodies

# The CSRF token is read from the DOM (not the cookie), so the cookie can be
# HttpOnly — one less thing a XSS could read.
CSRF_COOKIE_HTTPONLY = True

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'landing.context_processors.site_globals',
                'landing.context_processors.dash_roles',
            ],
        },
    },
]

WSGI_APPLICATION = 'dominio_website.wsgi.application'


# ============================================================
# DATABASE — SQLite is fine for a static landing
# ============================================================

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.environ.get('DJANGO_DB_PATH', BASE_DIR / 'db.sqlite3'),
        # gunicorn runs threaded workers (8 requests in flight), and SQLite's
        # default mode lets ONE writer block every reader. Without this the
        # first two overlapping chat turns produce "database is locked".
        'OPTIONS': {
            # How long a connection waits for a lock before giving up. Django's
            # default is 5s; a chat turn that queued behind a lead insert on a
            # busy second should wait, not 500.
            'timeout': 20,
            # BEGIN IMMEDIATE takes the write lock up front instead of
            # upgrading a read lock mid-transaction — the upgrade is where
            # SQLite deadlocks two writers and errors out without retrying.
            'transaction_mode': 'IMMEDIATE',
            # WAL: readers never block on the writer and vice versa. NORMAL
            # sync is safe with WAL (a crash loses at most the last
            # transaction, never corrupts) and avoids an fsync per write.
            'init_command': 'PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;',
        },
    }
}


# ============================================================
# PASSWORDS / I18N / TIMEZONE
# ============================================================

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Password-reset links die after one hour (Django default is 3 days).
PASSWORD_RESET_TIMEOUT = int(os.environ.get('DJANGO_PASSWORD_RESET_TIMEOUT', '3600'))

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/Puerto_Rico'
USE_I18N = True
USE_TZ = True


# ============================================================
# STATIC FILES — WhiteNoise handles serving in production
# ============================================================

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Compressed + immutable hashed filenames for production caching
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        if not DEBUG else 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}


# ============================================================
# DEFAULTS / SECURITY (production only)
# ============================================================

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

if not DEBUG:
    # HTTPS / cookies
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

    # HSTS (start small, ramp up after confirming TLS works for all subdomains)
    SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_HSTS_SECONDS', '3600'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = False

    # Misc hardening
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    SECURE_REFERRER_POLICY = 'same-origin'


# ============================================================
# LOGGING — stdout so systemd/journalctl captures it
# ============================================================

# Who gets told when the site throws a 500. Without this, an unhandled error in
# production is only a line in journald that nobody reads — the first signal is
# a customer complaining, or silence where a sale should have been.
# Uses Django's built-in AdminEmailHandler: no extra service, no new dependency.
ADMINS = [
    (name.strip(), addr.strip())
    for name, addr in (
        pair.split(':', 1) if ':' in pair else ('DOMINIO', pair)
        for pair in os.environ.get(
            'DJANGO_ADMINS', os.environ.get('DJANGO_CONTACT_NOTIFY_EMAIL', '')).split(',')
        if pair.strip()
    )
]
# DEFAULT_FROM_EMAIL is defined further down, so read the env directly here.
SERVER_EMAIL = os.environ.get(
    'DJANGO_SERVER_EMAIL',
    os.environ.get('DJANGO_DEFAULT_FROM_EMAIL', 'DOMINIO <hola@dominiopr.com>'))

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'require_debug_false': {'()': 'django.utils.log.RequireDebugFalse'},
    },
    'formatters': {
        # Timestamp + level + logger, so journald lines are actually diagnosable.
        'standard': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'standard',
        },
        'mail_admins': {
            'level': 'ERROR',
            'filters': ['require_debug_false'],
            'class': 'django.utils.log.AdminEmailHandler',
            'include_html': True,
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': os.environ.get('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
        # Unhandled 500s.
        'django.request': {
            'handlers': ['console', 'mail_admins'],
            'level': 'ERROR',
            'propagate': False,
        },
        # Our own code: a failed provisioning or a Stripe error must reach a human.
        'landing': {
            'handlers': ['console', 'mail_admins'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}


# ============================================================
# EMAIL — contact form notifications
# In DEBUG we print emails to the console (no SMTP needed locally).
# In production we use SMTP (Gmail) with an App Password from the .env.
# ============================================================

EMAIL_BACKEND = os.environ.get(
    'DJANGO_EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend' if DEBUG
    else 'django.core.mail.backends.smtp.EmailBackend',
)
EMAIL_HOST = os.environ.get('DJANGO_EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('DJANGO_EMAIL_PORT', '587'))
EMAIL_USE_TLS = os.environ.get('DJANGO_EMAIL_USE_TLS', 'True').lower() in ('true', '1', 'yes')
EMAIL_HOST_USER = os.environ.get('DJANGO_EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('DJANGO_EMAIL_HOST_PASSWORD', '')
# Never let a slow/dead SMTP host pin a Gunicorn worker (512MB box, 2 workers).
EMAIL_TIMEOUT = int(os.environ.get('DJANGO_EMAIL_TIMEOUT', '10'))
DEFAULT_FROM_EMAIL = os.environ.get(
    'DJANGO_DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'no-reply@dominiopr.com'
)
# Where new contact-form leads are emailed.
CONTACT_NOTIFY_EMAIL = os.environ.get('DJANGO_CONTACT_NOTIFY_EMAIL', 'creatudominiopr@gmail.com')
# Lead/booking emails: after the syntax + disposable-domain checks, ask DNS
# (DNS-over-HTTPS, 3 s timeout, cached 24 h per domain) whether the domain can
# receive mail at all. Fail-open: a slow or unreachable resolver never blocks a
# lead. Always off under `manage.py test` so the suite never hits the network.
DOMINIO_EMAIL_DNS_CHECK = (
    os.environ.get('DOMINIO_EMAIL_DNS_CHECK', 'True').lower() in ('true', '1', 'yes')
    and not TESTING
)


# ============================================================
# HUMAN CHECK — hCaptcha on the chat widgets, the demo and the public forms.
# Both keys empty = OFF everywhere: the site behaves exactly as without it.
# Always off under `manage.py test` (tests enable it with override_settings).
# A solved challenge on the chat yields a signed "human pass" bound to the
# tenant slug + the visitor id, valid HUMAN_PASS_TTL seconds and at most
# HUMAN_PASS_MAX_MESSAGES chat messages (see landing/human.py).
# ============================================================

HCAPTCHA_SITEKEY = '' if TESTING else os.environ.get('HCAPTCHA_SITEKEY', '').strip()
HCAPTCHA_SECRET = '' if TESTING else os.environ.get('HCAPTCHA_SECRET', '').strip()
HUMAN_PASS_TTL = int(os.environ.get('HUMAN_PASS_TTL', '7200'))
HUMAN_PASS_MAX_MESSAGES = int(os.environ.get('HUMAN_PASS_MAX_MESSAGES', '30'))


# ============================================================
# ANALYTICS — Google Analytics 4 (loaded client-side, only after consent)
# Set GA_MEASUREMENT_ID in the .env (e.g. G-XXXXXXXXXX). Empty = no tracking.
# ============================================================

GA_MEASUREMENT_ID = os.environ.get('GA_MEASUREMENT_ID', '')


# ============================================================
# AI AGENT — the "Ask DOMINIO" website chat assistant.
# Inference runs on Anthropic's cloud (not on this server), so it does not
# load the Lightsail box. Set ANTHROPIC_API_KEY in the .env to switch it on;
# empty key = widget is hidden and the endpoint returns a friendly notice.
# ============================================================

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
# Hard ceiling on total chat requests across ALL visitors per day. This is the
# defense per-IP rate limits can't provide: it caps the worst-case daily API
# spend even under a distributed/botnet attack. Tune to your budget.
DOMINIO_AGENT_DAILY_CAP = int(os.environ.get('DOMINIO_AGENT_DAILY_CAP', '2000'))
# New chat conversations one IP may start per tenant per day (0 = off). Each
# new Conversation is the billable unit of the packages, so this caps what a
# bot with the public widget key can inflate; 15 leaves room for an office NAT.
DOMINIO_NEW_CONVERSATIONS_PER_IP_DAY = int(os.environ.get('DOMINIO_NEW_CONVERSATIONS_PER_IP_DAY', '15'))
# The public /api/demo/ endpoint needs its OWN budget. Sharing the tenants'
# counter meant an anonymous visitor could exhaust it and take every paying
# client's agent offline for the rest of the day, on DOMINIO's token bill.
DOMINIO_DEMO_DAILY_CAP = int(os.environ.get('DOMINIO_DEMO_DAILY_CAP', '150'))
DOMINIO_DEMO_IP_DAILY_CAP = int(os.environ.get('DOMINIO_DEMO_IP_DAILY_CAP', '20'))
# Per-tenant, per-day ceiling on leads, bookings and human escalations a chat
# can create. The widget token is public by construction, so this is what
# stops a scripted bot from filling one client's CRM and phone with junk.
DOMINIO_TENANT_ACTION_DAILY_CAP = int(os.environ.get('DOMINIO_TENANT_ACTION_DAILY_CAP', '40'))
# Haiku is the right tier for a fast, low-cost FAQ/sales chat. Haiku 5.5 is
# ~10x cheaper than 4.5 and follows the rules better. ANTHROPIC_MODEL_FAST is
# deliberately NOT read anymore: the shared .env pins it to Haiku 4.5 for other
# apps, which would silently keep this agent on the old, pricier model.
DOMINIO_AGENT_MODEL = os.environ.get('DOMINIO_AGENT_MODEL') or 'claude-haiku-5-5'
# Haiku 5.5 thinks before answering; effort sets how much. 'low' keeps chat fast
# and cheap; raise to 'medium' if the agent starts skipping business rules.
DOMINIO_AGENT_EFFORT = os.environ.get('DOMINIO_AGENT_EFFORT', 'low')
# Reply cap per API call. Thinking counts against it on Haiku 5.5, so it needs
# headroom above the ~200-token replies or answers get cut off.
DOMINIO_AGENT_MAX_TOKENS = int(os.environ.get('DOMINIO_AGENT_MAX_TOKENS', '1500'))
# Blended $/MTok fallback, used only for a model missing from the table below.
DOMINIO_COST_PER_MTOK = float(os.environ.get('DOMINIO_COST_PER_MTOK', '0.10'))
# Anthropic list prices, USD per million tokens, matched by model-id prefix
# (longest prefix wins). Cache write = 5-minute TTL rate. Update when prices change.
DOMINIO_MODEL_PRICES = {
    'claude-haiku-5-5': {'input': 0.10, 'output': 0.50, 'cache_write': 0.125, 'cache_read': 0.01},
    'claude-haiku-4-5': {'input': 1.00, 'output': 5.00, 'cache_write': 1.25, 'cache_read': 0.10},
    'claude-sonnet-5-5': {'input': 2.00, 'output': 10.00, 'cache_write': 2.50, 'cache_read': 0.20},
}
# Cap how much history a single conversation can send back to the API, to
# bound cost/abuse from a public widget.
DOMINIO_AGENT_MAX_TURNS = int(os.environ.get('DOMINIO_AGENT_MAX_TURNS', '12'))
# M-12: which model provider the agent engine uses (only 'anthropic' today).
DOMINIO_AGENT_PROVIDER = os.environ.get('DOMINIO_AGENT_PROVIDER', 'anthropic')
# Phase 8 knowledge ingest (website scan + uploaded files), run by the
# process_knowledge cron, never inside a web request. Staff-only since Phase I:
# DOMINIO loads and maintains every tenant's knowledge, so the ceilings are
# high. Pages per scan, wall-clock seconds per crawl, and the upload ceiling
# (the file is read in memory only; the upload view uses its own bounded
# in-memory handler, so the global DATA_UPLOAD_MAX_MEMORY_SIZE above stays at
# 1 MB for every other view). Raising the upload size also needs
# client_max_body_size in nginx (deploy/nginx/dominio.conf).
DOMINIO_INGEST_MAX_PAGES = int(os.environ.get('DOMINIO_INGEST_MAX_PAGES', '500'))
DOMINIO_INGEST_TIME_BUDGET = int(os.environ.get('DOMINIO_INGEST_TIME_BUDGET', '1200'))
DOMINIO_INGEST_UPLOAD_MAX_MB = int(os.environ.get('DOMINIO_INGEST_UPLOAD_MAX_MB', '50'))
# Document text per source sent to the model (cost ceiling of one scan/upload;
# ~4 chars per token). landing/ingest.py clamps it to 8,000,000.
DOMINIO_INGEST_MAX_INPUT_CHARS = int(os.environ.get('DOMINIO_INGEST_MAX_INPUT_CHARS', '4000000'))
# Model and effort for scanning/distilling (NOT the chat): extraction quality
# matters and it runs a few times per tenant, so Sonnet at medium. Effort is
# validated against landing.agent._EFFORT_LEVELS; invalid values fall back to
# medium. The chat and everything else keep DOMINIO_AGENT_MODEL.
DOMINIO_INGEST_MODEL = os.environ.get('DOMINIO_INGEST_MODEL') or 'claude-sonnet-5-5'
DOMINIO_INGEST_EFFORT = os.environ.get('DOMINIO_INGEST_EFFORT', 'medium')
# Physical ceiling for a tenant's ACTIVE knowledge (all sources). Publishing
# beyond it is refused: every chat question would carry a prompt that big. The
# 8k/40k figures on the knowledge page are advice only.
DOMINIO_KNOWLEDGE_HARD_MAX_CHARS = int(os.environ.get('DOMINIO_KNOWLEDGE_HARD_MAX_CHARS', '160000'))


# ============================================================
# PAYMENTS — Stripe subscriptions (M-01). Empty keys = signup keeps the
# email-coordination flow (same graceful-degradation pattern as the agent).
# Card data never touches this server: hosted Checkout + signed webhooks.
# ============================================================

STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
# Price ids per plan/period, e.g. STRIPE_PRICE_STARTER_MONTHLY=price_123
STRIPE_PRICES = {
    f'{plan}:{period}': os.environ.get(f'STRIPE_PRICE_{plan.upper()}_{period.upper()}', '')
    for plan in ('starter', 'pro', 'scale')
    for period in ('monthly', 'annual')
}
# One-time setup fee ("instalación y configuración") per plan, charged in the
# same Checkout as the first period. e.g. STRIPE_PRICE_PRO_SETUP=price_456
for _plan in ('starter', 'pro', 'scale'):
    STRIPE_PRICES[f'{_plan}:setup'] = os.environ.get(f'STRIPE_PRICE_{_plan.upper()}_SETUP', '')
STRIPE_PRICES = {k: v for k, v in STRIPE_PRICES.items() if v}

# Sales tax (IVU). Prices are advertised WITHOUT tax, so the tax is added on
# top at Checkout. Set to a Stripe Tax Rate id (`manage.py stripe_bootstrap
# --tax-rate 4`) and every line item — subscription and setup fee — carries it.
# Empty = no tax line, which is the pre-2026-08 behaviour: keep it empty only
# while you genuinely owe none, because Stripe will not add it retroactively.
STRIPE_TAX_RATE_ID = os.environ.get('STRIPE_TAX_RATE_ID', '')


# ============================================================
# NOTIFICATIONS — Twilio SMS (M-06). Outbound alerts to tenants and phone-only
# booking texts; unset = silent no-op (email remains the always-on channel).
# Twilio is SMS only: the visitors' WhatsApp channel goes direct through the
# Cloud API of Meta (META_* below, landing/whatsapp.py), decision 2026-10-14.
# ============================================================

TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_FROM_SMS = os.environ.get('TWILIO_FROM_SMS', '')


# ============================================================
# WHATSAPP (Phase G) — the visitors' WhatsApp chat through Meta's Cloud API
# directly (no Twilio, no SDK). One DOMINIO Business app + System User token
# for every tenant; per tenant only a ChannelAccount row (phone_number_id).
# All empty = the webhook answers 503 and nothing is ever sent.
# ============================================================

# Permanent System User token with whatsapp_business_messaging (+ _management).
META_WA_ACCESS_TOKEN = os.environ.get('META_WA_ACCESS_TOKEN', '')
# App Secret of the Meta app: every webhook POST is HMAC-SHA256 signed with it.
META_APP_SECRET = os.environ.get('META_APP_SECRET', '')
# The string pasted in the webhook config; Meta echoes it on the GET handshake.
META_WA_VERIFY_TOKEN = os.environ.get('META_WA_VERIFY_TOKEN', '')
# Graph API version pinned for sends (raise it when Meta deprecates it).
META_GRAPH_VERSION = os.environ.get('META_GRAPH_VERSION', 'v23.0')
# Approved UTILITY template names for business-initiated messages (outside
# the 24 h window). Empty = fall back to SMS (Twilio) or nothing.
#   reminder: {{1}} negocio, {{2}} fecha/hora, {{3}} local
#   status:   {{1}} negocio, {{2}} fecha/hora, {{3}} local, {{4}} estado
META_WA_TEMPLATE_REMINDER = os.environ.get('META_WA_TEMPLATE_REMINDER', '')
META_WA_TEMPLATE_STATUS = os.environ.get('META_WA_TEMPLATE_STATUS', '')
# Key of the salted hash that stands in for the visitor's phone number in
# conversations and opt-outs. Falls back to SECRET_KEY; set it once and
# NEVER rotate it (every contact's history would split from their opt-out).
DOMINIO_PHONE_HASH_KEY = os.environ.get('DOMINIO_PHONE_HASH_KEY', '')
# Messages one WhatsApp contact may send a tenant per day before the agent
# stops answering (a fixed notice once). Abuse brake, like the widget's
# per-visitor share of the daily cap.
DOMINIO_WA_CONTACT_DAILY_CAP = int(os.environ.get('DOMINIO_WA_CONTACT_DAILY_CAP', '40'))


# ============================================================
# SOCIAL PUBLISHING (M-20) — Instagram/Facebook via the Meta Graph API and
# TikTok photo posts via the Content Posting API. No SDKs: plain HTTPS, same
# pattern as Stripe. A platform with no credentials is refused loudly by
# `publish_due` instead of half-publishing.
# ============================================================

SOCIAL_GRAPH_VERSION = os.environ.get('SOCIAL_GRAPH_VERSION', 'v21.0')
# One long-lived Page access token covers both Facebook and the Instagram
# Business account linked to that Page.
SOCIAL_META_ACCESS_TOKEN = os.environ.get('SOCIAL_META_ACCESS_TOKEN', '')
SOCIAL_IG_USER_ID = os.environ.get('SOCIAL_IG_USER_ID', '')
SOCIAL_FB_PAGE_ID = os.environ.get('SOCIAL_FB_PAGE_ID', '')
SOCIAL_TIKTOK_ACCESS_TOKEN = os.environ.get('SOCIAL_TIKTOK_ACCESS_TOKEN', '')
# An app that has not passed TikTok's content-posting audit may ONLY publish
# with SELF_ONLY; anything else is rejected at init.
SOCIAL_TIKTOK_PRIVACY_LEVEL = os.environ.get(
    'SOCIAL_TIKTOK_PRIVACY_LEVEL', 'PUBLIC_TO_EVERYONE')
# Attempts before a post is marked failed, and the ceiling on the exponential
# backoff between them (8 attempts capped at 30 min ~= 1h of retrying).
SOCIAL_MAX_ATTEMPTS = int(os.environ.get('SOCIAL_MAX_ATTEMPTS', '8'))
SOCIAL_BACKOFF_CAP_SECONDS = int(os.environ.get('SOCIAL_BACKOFF_CAP_SECONDS', '1800'))
