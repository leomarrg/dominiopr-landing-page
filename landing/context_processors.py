from django.conf import settings


def site_globals(request):
    """Expose a few settings to every template (e.g. the GA measurement id)."""
    return {
        'GA_MEASUREMENT_ID': getattr(settings, 'GA_MEASUREMENT_ID', ''),
        # Only render the chat widget when an API key is actually configured.
        'AI_CHAT_ENABLED': bool(getattr(settings, 'ANTHROPIC_API_KEY', '')),
    }
