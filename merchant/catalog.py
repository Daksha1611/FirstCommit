"""The procurement catalogue: what a company buys, and what it costs.

Enterprise procurement rather than a corner shop, because that is what this
system is actually shaped like. A budget signed once at the top, split by
department, spent with approved suppliers, every rupee accounted for - that is
the delegation tree, the cumulative ledger and the audit chain described in
business terms rather than cryptographic ones.

Prices are the company's negotiated reference; each supplier quotes against them
in offers.py.

The agent reads supplier-written text here, which makes this module untrusted
input - and is why the agents that read it hold no payment capability.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

RUPEE = 100  # paise

CATALOGUE_OWNER = "Meridian Industries · Procurement"


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    unit: str
    price_paise: int
    category: str
    description: str

    @property
    def price_rupees(self) -> float:
        """Display only. Never compute with this - see policy.py on floats."""
        return self.price_paise / RUPEE


CATALOG: tuple[Product, ...] = (
    Product("LAP-STD-1", "Standard laptop, 14in i5/16GB", "unit", 62_000 * RUPEE,
            "hardware", "Issued to non-engineering staff. Three year warranty."),
    Product("LAP-PRO-1", "Engineering laptop, 16in/32GB", "unit", 148_000 * RUPEE,
            "hardware", "For build and simulation workloads. Three year warranty."),
    Product("MON-27Q-1", "27in QHD monitor", "unit", 21_500 * RUPEE,
            "hardware", "Height adjustable stand, USB-C passthrough."),
    Product("HDS-ANC-1", "Noise cancelling headset", "unit", 9_800 * RUPEE,
            "hardware", "Certified for the support floor."),
    Product("CHR-ERG-1", "Ergonomic task chair", "unit", 18_400 * RUPEE,
            "furniture", "Meets the occupational health standard for desk use."),
    Product("DSK-ADJ-1", "Sit-stand desk, 1400mm", "unit", 27_900 * RUPEE,
            "furniture", "Electric column, 80kg rated."),
    Product("CBL-CAT6-1", "Cat6 patch cable, 2m", "pack of 10", 2_100 * RUPEE,
            "network", "Shielded, factory terminated."),
    Product("KVM-DCK-1", "USB-C docking station", "unit", 12_600 * RUPEE,
            "hardware", "Dual display, 100W power delivery."),
    Product("TNR-LSR-1", "Laser toner cartridge", "unit", 5_400 * RUPEE,
            "consumables", "Genuine cartridge, approximately 3,000 pages."),
    Product("STA-A4-5", "A4 paper", "box of 5 reams", 1_450 * RUPEE,
            "consumables", "80gsm, recycled content."),
    Product("SRV-RCK-1", "Rack server, 1U", "unit", 310_000 * RUPEE,
            "infrastructure", "Dual socket, three year on-site support."),
    Product("UPS-1KV-1", "UPS, 1kVA line interactive", "unit", 16_800 * RUPEE,
            "infrastructure", "Rack mount, network management card."),
)

BY_SKU = {p.sku: p for p in CATALOG}


def browse(category: str | None = None) -> tuple[Product, ...]:
    if category is None:
        return CATALOG
    return tuple(p for p in CATALOG if p.category == category)


def search(query: str) -> tuple[Product, ...]:
    """Plain substring match over name, category and description.

    Deliberately dumb. A clever retriever would be a second thing that can be
    wrong, and the point of this system is what happens *after* retrieval.
    """
    q = query.lower().strip()
    if not q:
        return ()
    return tuple(
        p for p in CATALOG
        if q in p.name.lower() or q in p.category.lower() or q in p.description.lower()
    )


def get(sku: str) -> Product:
    if sku not in BY_SKU:
        raise KeyError(f"no such product: {sku}")
    return BY_SKU[sku]


def as_json(products: tuple[Product, ...] | None = None) -> list[dict]:
    """The agent-readable view."""
    return [asdict(p) for p in (products if products is not None else CATALOG)]


def price_cart(items: dict[str, int]) -> int:
    """Total a requisition of {sku: quantity}, in paise.

    Integer arithmetic end to end. Raises on an unknown sku rather than skipping
    it, because a requisition that silently drops a line is one whose total does
    not match what was approved.
    """
    total = 0
    for sku, qty in items.items():
        if qty <= 0:
            raise ValueError(f"quantity for {sku} must be positive, got {qty}")
        total += get(sku).price_paise * qty
    return total
