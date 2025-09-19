"""The catalogue as a prompt, with supplier text marked.

There is exactly one of these, and that is the point.

`agent/tools.py` marks supplier-written text three careful ways at the tool
boundary. Then `planner_node` and `standing_node` each grew their own
`catalogue_brief()` that read `catalog.CATALOG` directly and rendered product
descriptions straight into a prompt - unmarked, in an instruction position.
Identical code in two files, neither importing the module where the rule lives.

A marking rule that can be bypassed by not importing it is not a rule. So the
only way to put the catalogue in front of a model now goes through here.

Lives in agent/utils rather than merchant/ so the data layer does not have to
import the agent layer to describe itself.
"""

from __future__ import annotations

from merchant import catalog

from .provenance import INSTRUCTION, mark

# Kept short. A brief is for choosing, not for reading - and every extra line of
# supplier prose is another line of untrusted text in the context window.
DESCRIPTION_CHARS = 70


def catalogue_brief(products=None, *, with_descriptions: bool = True) -> str:
    """Render the catalogue for a prompt. Descriptions are datamarked.

    `with_descriptions=False` drops supplier prose entirely - the right choice
    where a caller needs SKUs and prices but has no reason to read what a
    supplier wrote about them. Not sending untrusted text beats marking it.
    """
    lines = []
    for product in products or catalog.CATALOG:
        row = f"  {product.sku:<12} {product.name:<34} {product.price_paise:>9}p  {product.category}"
        if with_descriptions:
            row += f"\n      {mark(product.description[:DESCRIPTION_CHARS])}"
        lines.append(row)
    return "\n".join(lines)


def catalogue_prompt_block(products=None, *, with_descriptions: bool = True) -> str:
    """The brief plus the standing instruction about marked text."""
    brief = catalogue_brief(products, with_descriptions=with_descriptions)
    return f"{brief}\n\n{INSTRUCTION}" if with_descriptions else brief
