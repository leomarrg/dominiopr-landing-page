"""
Django settings for dominio_website project.
"""

import os
from pathlib import Path

# Optional: load .env file in local development
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


BASE_DIR = Path(__file__).resolve().parent.parent


# ============================================================
# CORE
# ============================================================

SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-qhc=x5)p53y(abfe#s666@l+l#a8hvy0(5xh29pylt(&)u(1!='
)

DEBUG = os.environ.get('DJANGO_DEBUG', 'True').lower() in ('true', '1', 'yes')

# ALLOWED_HOSTS: leading dot matches the domain AND all subdomains.
# e.g. ".dominiopr.com" matches dominiopr.com, www.dominiopr.com,
# registratepr.dominiopr.com, etc.
_default_hosts = '.dominiopr.com,localhost,127.0.0.1' if not DEBUG else '*'
ALLOWED_HOSTS = [h.strip() for h in os.environ.get('DJANGO_ALLOWED_HOSTS', _default_hosts).split(',') if h.strip()]

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get(
        'DJANGO_CSRF_TRUSTED_ORIGINS',
        'https://dominiopr.com,https://www.dominiopr.com,https://*.dominiopr.com,https://*.ngrok-free.app'
    ).split(',') if o.strip()
]


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
    'landing',
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
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

if DEBUG:
    try:
        import django_browser_reload  # noqa: F401
        MIDDLEWARE.append('django_browser_reload.middleware.BrowserReloadMiddleware')
    except ImportError:
        pass

ROOT_URLCONF = 'dominio_website.urls'

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

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
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
DEFAULT_FROM_EMAIL = os.environ.get(
    'DJANGO_DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'no-reply@dominiopr.com'
)
# Where new contact-form leads are emailed.
CONTACT_NOTIFY_EMAIL = os.environ.get('DJANGO_CONTACT_NOTIFY_EMAIL', 'creatudominiopr@gmail.com')


# ============================================================
# ANALYTICS — Google Analytics 4 (loaded client-side, only after consent)
# Set GA_MEASUREMENT_ID in the .env (e.g. G-XXXXXXXXXX). Empty = no tracking.
# ============================================================

GA_MEASUREMENT_ID = os.environ.get('GA_MEASUREMENT_ID', '')
