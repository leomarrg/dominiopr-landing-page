from django.conf import settings


def site_globals(request):
    """Expose a few settings to every template (e.g. the GA measurement id)."""
    return {
        'GA_MEASUREMENT_ID': getattr(settings, 'GA_MEASUREMENT_ID', ''),
    }
