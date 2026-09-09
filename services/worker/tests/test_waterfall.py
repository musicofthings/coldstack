from coldstack.enrich.waterfall import ProviderStats, order_providers


def test_orders_by_cost_per_hit_not_sticker_price():
    """A pricier provider that actually hits beats a cheap one that misses."""
    stats = {
        "cheap_but_useless": ProviderStats(attempts=100, hits=5, unit_cost_usd=0.002),
        "pricey_but_good":   ProviderStats(attempts=100, hits=90, unit_cost_usd=0.010),
    }
    assert order_providers(stats)[0] == "pricey_but_good"


def test_cost_per_hit_maths():
    s = ProviderStats(attempts=100, hits=50, unit_cost_usd=0.010)
    assert s.hit_rate == 0.5
    assert s.cost_per_hit == 0.02


def test_zero_hit_provider_is_floored_not_infinite():
    s = ProviderStats(attempts=100, hits=0, unit_cost_usd=0.01)
    assert s.cost_per_hit == 0.01 / 0.05      # HIT_RATE_FLOOR, not ZeroDivisionError


def test_unseen_provider_uses_a_prior():
    assert ProviderStats(unit_cost_usd=0.01).hit_rate == 0.35
