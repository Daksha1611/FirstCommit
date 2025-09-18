"""One product, several offers. The reason there is a decision to make.

Two properties are deliberate.

**Prices differ per seller**, so "cheapest" and "best reviewed" pull apart and the
three shopper strategies have something to disagree about. With one offer per
product they cannot diverge, and the fan-out is decorative - which is exactly
what the first live run showed when all three carts came back identical.

**No seller carries everything.** Coverage is partial, so a realistic cart spans
several sellers. That is what makes delegation necessary rather than a flourish:
four sellers is four payments, and each should carry an authority capped at that
seller's subtotal rather than the whole mandate.

Derivation is deterministic - a hash of (sku, seller), never a random number - so
a demo run and a test see the same market.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .catalog import Product, get as get_product
from .sellers import SELLERS, Seller, get as get_seller


def _spread(sku: str, seller_id: str, salt: str = "") -> int:
    """A stable 0-99 derived from the pair. Same market every run."""
    material = f"{sku}:{seller_id}:{salt}".encode()
    return hashlib.sha256(material).digest()[0] % 100


@dataclass(frozen=True)
class Offer:
    sku: str
    seller_id: str
    price_paise: int
    stock: int
    delivery_days: int
    min_quantity: int

    @property
    def in_stock(self) -> bool:
        return self.stock > 0

    def as_dict(self) -> dict:
        seller = get_seller(self.seller_id)
        return {
            "sku": self.sku,
            "seller_id": self.seller_id,
            "seller_name": seller.name,
            "price_paise": self.price_paise,
            "stock": self.stock,
            "delivery_days": self.delivery_days,
            "min_quantity": self.min_quantity,
            "seller_rating": seller.rating,
            "seller_review_count": seller.review_count,
            "seller_established": seller.is_established,
        }


def _carries(product: Product, seller: Seller) -> bool:
    """Partial coverage, so carts span sellers.

    The established sellers carry most of the catalogue; the newest carries
    least, which is also why it is cheap.
    """
    coverage = {
        "meridian-systems": 92,
        "apex-technologies": 78,
        "northgate-supply": 70,
        "vector-distribution": 62,
        "clearline-traders": 55,
    }[seller.id]
    return _spread(product.sku, seller.id, "carries") < coverage


def _price(product: Product, seller: Seller) -> int:
    """Reference price scaled by the seller, nudged so ranks are not uniform.

    Integer paise throughout - see policy.py on why money is never a float.
    """
    jitter = 0.97 + (_spread(product.sku, seller.id, "price") % 7) * 0.01
    return int(round(product.price_paise * seller.price_factor * jitter))


def offers_for(sku: str) -> tuple[Offer, ...]:
    """Every offer for one product, cheapest first."""
    product = get_product(sku)
    found = []
    for seller in SELLERS:
        if not _carries(product, seller):
            continue
        stock = 0 if _spread(sku, seller.id, "stock") < 8 else 20 + _spread(sku, seller.id, "qty")
        found.append(
            Offer(
                sku=sku,
                seller_id=seller.id,
                price_paise=_price(product, seller),
                stock=stock,
                delivery_days=seller.delivery_days,
                min_quantity=seller.min_order_quantity,
            )
        )
    return tuple(sorted(found, key=lambda o: o.price_paise))


def available_offers(sku: str, quantity: int = 1) -> tuple[Offer, ...]:
    """Offers that can actually fulfil this quantity."""
    return tuple(
        o for o in offers_for(sku)
        if o.in_stock and o.stock >= quantity and quantity >= o.min_quantity
    )


def best_price(sku: str, quantity: int = 1) -> Offer | None:
    offers = available_offers(sku, quantity)
    return offers[0] if offers else None


def best_reviewed(sku: str, quantity: int = 1) -> Offer | None:
    offers = available_offers(sku, quantity)
    if not offers:
        return None
    return max(offers, key=lambda o: get_seller(o.seller_id).review_count)


def price_line(sku: str, seller_id: str, quantity: int) -> int:
    """What one cart line costs at one seller. Raises if that offer is not real.

    The guard uses this to check that a proposed total matches offers that
    actually exist, rather than a price a shopper invented (T2T).
    """
    for offer in offers_for(sku):
        if offer.seller_id == seller_id:
            return offer.price_paise * quantity
    raise KeyError(f"{seller_id} does not offer {sku}")
