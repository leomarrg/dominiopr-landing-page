"""AI usage accounting: what each Anthropic call costs.

Prices live in settings.DOMINIO_MODEL_PRICES (USD per million tokens, matched
by model-id prefix). A model missing from the table falls back to the blended
DOMINIO_COST_PER_MTOK rate on all tokens, and logs a warning so the table gets
updated.
"""
import logging
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)

_MILLION = Decimal(1_000_000)


def _prices_for(model):
    table = getattr(settings, 'DOMINIO_MODEL_PRICES', {}) or {}
    matches = [prefix for prefix in table if model and model.startswith(prefix)]
    return table[max(matches, key=len)] if matches else None


def cost_for(model, usage):
    """USD cost (Decimal) of a usage dict from landing.agent.new_usage()."""
    def tok(key):
        return Decimal(usage.get(key, 0) or 0)

    prices = _prices_for(model)
    if prices is None:
        logger.warning('No price for model %r; using DOMINIO_COST_PER_MTOK', model)
        blended = Decimal(str(getattr(settings, 'DOMINIO_COST_PER_MTOK', 0.10)))
        total = sum(tok(k) for k in ('input_tokens', 'output_tokens',
                                      'cache_read_input_tokens', 'cache_creation_input_tokens'))
        return (total * blended / _MILLION).quantize(Decimal('0.000001'))

    def rate(key):
        return Decimal(str(prices.get(key, 0)))

    cost = (tok('input_tokens') * rate('input')
            + tok('output_tokens') * rate('output')
            + tok('cache_read_input_tokens') * rate('cache_read')
            + tok('cache_creation_input_tokens') * rate('cache_write')) / _MILLION
    return cost.quantize(Decimal('0.000001'))
