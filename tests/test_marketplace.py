"""The marketplace: several sellers, real trade-offs, no single source.

These are the properties the rest of L1 rests on. If any of them stops holding,
the fan-out becomes decorative again and delegation has nothing to split.
"""

import pytest

from agent.tools import ToolSurface
from merchant import catalog, offers, reviews, sellers

BASKET = {"LAP-STD-1": 4, "MON-27Q-1": 2, "CHR-ERG-1": 1, "DSK-ADJ-1": 1, "KVM-DCK-1": 1}


@pytest.fixture
def tools():
    return ToolSurface(client=None, shopper_token="s", payer_token="p")


# --- the market has choices in it -----------------------------------------


def test_products_have_several_offers():
    """One offer per product means one correct cart and no decision to make."""
    assert len(offers.offers_for("MON-27Q-1")) >= 3


def test_prices_actually_differ():
    prices = {o.price_paise for o in offers.offers_for("MON-27Q-1")}
    assert len(prices) > 1


def test_offers_are_sorted_cheapest_first():
    got = [o.price_paise for o in offers.offers_for("MON-27Q-1")]
    assert got == sorted(got)


def test_market_is_deterministic():
    """A demo run and a test must see the same market."""
    first = [(o.seller_id, o.price_paise) for o in offers.offers_for("SRV-RCK-1")]
    second = [(o.seller_id, o.price_paise) for o in offers.offers_for("SRV-RCK-1")]
    assert first == second


def test_cheapest_is_not_best_reviewed():
    """If they were the same seller there would be no trade-off to judge."""
    cheap = offers.best_price("MON-27Q-1", 2)
    reviewed = offers.best_reviewed("MON-27Q-1", 2)
    assert cheap.seller_id != reviewed.seller_id


# --- the property delegation depends on ------------------------------------


def test_no_single_seller_carries_the_basket():
    """A realistic cart must span sellers, or splitting it is theatre."""
    for seller in sellers.SELLERS:
        carried = sum(
            1 for sku, qty in BASKET.items()
            if any(o.seller_id == seller.id for o in offers.available_offers(sku, qty))
        )
        assert carried < len(BASKET), f"{seller.id} carries the whole basket"


def test_cheapest_basket_spans_several_sellers():
    used = set()
    for sku, qty in BASKET.items():
        available = offers.available_offers(sku, qty)
        if available:
            used.add(available[0].seller_id)
    assert len(used) >= 2


# --- constraints that are real --------------------------------------------


def test_minimum_quantity_is_enforced():
    """Bulk Bazaar is cheapest and unusable for a 4 kg order."""
    small = offers.available_offers("LAP-STD-1", 4)
    assert all(o.seller_id != "clearline-traders" for o in small)


def test_out_of_stock_offers_are_excluded():
    for sku in catalog.BY_SKU:
        for offer in offers.available_offers(sku, 1):
            assert offer.in_stock


def test_price_line_rejects_a_seller_that_does_not_carry_it():
    """The basis of the guard's check against invented prices (T2T)."""
    # Find a real gap rather than assuming one - coverage is derived, so which
    # seller lacks which product is not something a test should hardcode.
    gap = next(
        (sku, seller.id)
        for sku in catalog.BY_SKU
        for seller in sellers.SELLERS
        if all(o.seller_id != seller.id for o in offers.offers_for(sku))
    )
    with pytest.raises(KeyError):
        offers.price_line(gap[0], gap[1], 1)


# --- reputation is a judgement, not a fact ---------------------------------


def test_a_high_rating_on_few_reviews_is_not_established():
    assert sellers.get("clearline-traders").rating > sellers.get("vector-distribution").rating
    assert sellers.get("clearline-traders").is_established is False


def test_review_text_reflects_fulfilment_not_stars():
    """The text says what the star hides."""
    poor = reviews.summarise("LAP-STD-1", "clearline-traders")
    good = reviews.summarise("LAP-STD-1", "meridian-systems")
    assert poor["fulfilment_rate"] < good["fulfilment_rate"]
    assert max(r["rating"] for r in poor["recent_reviews"]) <= min(
        r["rating"] for r in good["recent_reviews"]
    )


def test_review_bodies_are_named_untrusted():
    """Provenance starts at the field name; L2 makes it structural."""
    for review in reviews.summarise("MON-27Q-1", "vector-distribution")["recent_reviews"]:
        assert "body_untrusted" in review


def test_reviews_do_not_repeat_themselves():
    bodies = [r.body for r in reviews.reviews_for("LAP-STD-1", "northgate-supply")]
    assert len(set(bodies)) == len(bodies)


# --- the tool surface records what was seen --------------------------------


def test_find_offers_records_what_the_shopper_saw(tools):
    """The guard traces a proposed cart back to these."""
    tools.find_offers("LAP-STD-1")
    seen = tools.offers_seen["LAP-STD-1"]
    assert seen and all(isinstance(price, int) for _seller, price in seen)


def test_unknown_seller_is_reported_not_raised(tools):
    assert "error" in tools.seller_reputation("not-a-real-shop")


def test_shopper_has_the_marketplace_tools_and_still_cannot_pay(tools):
    names = {f.__name__ for f in tools.shopper_functions()}
    assert {"find_offers", "seller_reputation", "read_reviews"} <= names
    assert "checkout" not in names
